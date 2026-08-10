"""Time sensitive test, it waits for an epoch to pass."""

import asyncio

import pytest
from bot.rebalance import rebalance
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair
from stake.actions import create_stake, delegate_stake
from stake.constants import LAMPORTS_PER_SOL, STAKE_LEN
from stake_pool.state import StakePool, ValidatorList, ValidatorStakeInfo

ENDPOINT: str = "http://127.0.0.1:8899"


async def get_stake_pool(async_client, stake_pool_address) -> StakePool:
    resp = await async_client.get_account_info(stake_pool_address, commitment=Confirmed)
    data = resp.value.data if resp.value else b""
    return StakePool.decode(data)


async def get_validator_list(async_client, validator_list_address) -> ValidatorList:
    resp = await async_client.get_account_info(
        validator_list_address, commitment=Confirmed
    )
    data = resp.value.data if resp.value else b""
    return ValidatorList.decode(data)


def final_pool_stake(
    validator: ValidatorStakeInfo, previous_pool_stake: int, stake_rent_exemption: int
) -> int:
    """Stake the pool ends up with on a validator once its transient stake settles."""
    if validator.active_stake_lamports < previous_pool_stake:
        # a decrease takes effect right away, its transient stake goes back to the reserve
        return validator.active_stake_lamports
    # an increase parks the stake in the transient account, together with a rent exemption
    # that goes back to the reserve when it merges into the validator stake account
    return validator.active_stake_lamports + max(
        validator.transient_stake_lamports - stake_rent_exemption, 0
    )


@pytest.mark.asyncio
async def test_rebalance_equalized_stake_this_is_very_slow(
    async_client, validators, payer, stake_pool_addresses, waiter
):
    (stake_pool_address, validator_list_address, _) = stake_pool_addresses
    resp = await async_client.get_minimum_balance_for_rent_exemption(STAKE_LEN)
    stake_rent_exemption = resp.value

    # stake the first validator from outside of the pool, it takes an epoch to activate
    # and to show up in the total stake the validator votes with
    external_stake_lamports = 1_000 * LAMPORTS_PER_SOL
    external_stake = Keypair()
    await create_stake(
        async_client, payer, external_stake, payer.pubkey(), external_stake_lamports
    )
    await delegate_stake(
        async_client, payer, payer, external_stake.pubkey(), validators[0]
    )

    # Test case 1: the reserve is kept out first, the rest is evened out over the validators
    reserve_percent = 0.5
    await rebalance(
        ENDPOINT,
        stake_pool_address,
        payer,
        0.0,
        reserve_percent,
        equalize_stake=True,
    )

    # the pool total only moves when the pool is updated, so reading it after the
    # rebalance gives the very number the strategy worked with
    stake_pool = await get_stake_pool(async_client, stake_pool_address)
    retained_lamports = int(reserve_percent * stake_pool.total_lamports)
    validator_list = await get_validator_list(async_client, validator_list_address)
    num_validators = len(validator_list.validators)
    target_lamports = (stake_pool.total_lamports - retained_lamports) // num_validators
    staked_lamports = set()
    for validator in validator_list.validators:
        # on top of the stake, the transient stake account holds its own rent exemption,
        # which goes back to the reserve once it merges into the validator stake account
        staked_lamports.add(
            validator.active_stake_lamports
            + validator.transient_stake_lamports
            - stake_rent_exemption
        )
    # every validator gets the same, up to the rent exemptions paid by the reserve
    assert len(staked_lamports) == 1
    stake_per_validator = staked_lamports.pop()
    assert 0 <= target_lamports - stake_per_validator <= num_validators * stake_rent_exemption

    # the requested reserve is untouched
    resp = await async_client.get_balance(stake_pool.reserve_stake, commitment=Confirmed)
    assert resp.value >= retained_lamports + stake_rent_exemption

    # Test case 2: cap the stake per validator, everyone is evenly decreased down to it
    print("Waiting for next epoch")
    await waiter.wait_for_next_epoch(async_client)
    # the stake program rejects every stake action while the epoch rewards are paid out,
    # and the single block the waiter skips is not always enough for that to be over
    await asyncio.sleep(2.0)
    max_stake_percent = 0.1
    await rebalance(
        ENDPOINT,
        stake_pool_address,
        payer,
        0.0,
        0.0,
        equalize_stake=True,
        max_stake_percent=max_stake_percent,
    )

    stake_pool = await get_stake_pool(async_client, stake_pool_address)
    cap_lamports = int(max_stake_percent * stake_pool.total_lamports)
    validator_list = await get_validator_list(async_client, validator_list_address)
    for validator in validator_list.validators:
        # a decrease takes effect on the validator stake account right away
        assert validator.active_stake_lamports == cap_lamports
        # what the increase of the previous epoch staked above the cap is on its way back
        # to the reserve, together with the rent exemption of the transient account
        assert (
            validator.transient_stake_lamports - stake_rent_exemption
            == stake_per_validator - cap_lamports
        )
    pool_stake_per_validator = cap_lamports

    # Test case 3: with a cap that does not get in the way, the validator that is already
    # staked from outside gets less from the pool than the others
    print("Waiting for next epoch")
    await waiter.wait_for_next_epoch(async_client)
    await asyncio.sleep(2.0)
    await rebalance(
        ENDPOINT,
        stake_pool_address,
        payer,
        0.0,
        0.0,
        equalize_stake=True,
        max_stake_percent=0.9,
    )

    validator_list = await get_validator_list(async_client, validator_list_address)
    externally_staked = 0
    pool_staked = set()
    for validator in validator_list.validators:
        pool_stake = final_pool_stake(
            validator, pool_stake_per_validator, stake_rent_exemption
        )
        if validator.vote_account_address == validators[0]:
            externally_staked = pool_stake
        else:
            pool_staked.add(pool_stake)

    # the validators nobody else stakes are topped up equally...
    assert len(pool_staked) == 1
    evenly_staked = pool_staked.pop()
    # ...and further than the one that already votes with 1000 SOL of external stake
    assert externally_staked < evenly_staked
    # which leaves the total stakes far closer than the gap the external stake opened
    assert (
        abs(external_stake_lamports + externally_staked - evenly_staked)
        < external_stake_lamports // 4
    )
