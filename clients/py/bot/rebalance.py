import argparse
import asyncio
from typing import Dict, List, NamedTuple

import httpx
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.providers import async_http
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from stake.constants import LAMPORTS_PER_SOL, STAKE_LEN
from stake_pool.actions import (
    decrease_validator_stake,
    increase_validator_stake,
    update_stake_pool,
)
from stake_pool.constants import MINIMUM_ACTIVE_STAKE, MINIMUM_RESERVE_LAMPORTS
from stake_pool.state import StakePool, StakeStatus, ValidatorList, ValidatorStakeInfo


class InsecureAsyncHTTPProvider(async_http.AsyncHTTPProvider):
    def __init__(self, endpoint, timeout=10, extra_headers=None, proxy=None):
        super().__init__(endpoint, extra_headers=extra_headers)
        self.session = httpx.AsyncClient(timeout=timeout, proxy=proxy, verify=False)


class InsecureAsyncClient(AsyncClient):
    def __init__(
        self, endpoint, commitment=None, timeout=10, extra_headers=None, proxy=None
    ):
        super().__init__(endpoint, commitment, timeout, extra_headers, proxy)
        # Override the default provider with our custom one
        self._provider = InsecureAsyncHTTPProvider(
            endpoint, timeout, extra_headers, proxy
        )


DEFAULT_MAX_STAKE_PERCENT: float = 0.33
"""Share of the pool staked to a single validator when no other value is given."""


def sols(lamports: int) -> str:
    """Format an amount of lamports as a number of SOLs, lamports make for unreadable logs."""
    return f"{lamports / LAMPORTS_PER_SOL:,.2f}"


def to_sol(lamports: int) -> str:
    """Format an amount of lamports as SOLs, unit included."""
    return f"{sols(lamports)} SOLs"


class StakeChange(NamedTuple):
    """A stake movement planned for a single validator."""

    vote_account_address: Pubkey
    """Vote account of the validator to move stake for."""

    lamports: int
    """Lamports to move, positive to increase the stake, negative to decrease it."""


async def get_client(endpoint: str) -> AsyncClient:
    print(f"Connecting to network at {endpoint}")
    async_client = InsecureAsyncClient(endpoint=endpoint, commitment=Confirmed)
    total_attempts = 10
    current_attempt = 0
    while not await async_client.is_connected():
        if current_attempt == total_attempts:
            raise Exception("Could not connect to test validator")
        else:
            current_attempt += 1
        await asyncio.sleep(1)
    return async_client


def plan_even_split(
    validators: List[ValidatorStakeInfo],
    distributable_lamports: int,
    stake_rent_exemption: int,
) -> List[StakeChange]:
    """Plan a rebalance that splits the distributable lamports evenly over all the validators.

    The desired stake per validator is derived from the whole distributable amount, so the
    plan assumes that the reserve can fund every increase it asks for.
    """
    num_validators = len(validators)
    lamports_per_validator = distributable_lamports // num_validators
    num_increases = sum(
        [
            1
            for validator in validators
            if validator.transient_stake_lamports == 0
            and validator.active_stake_lamports < lamports_per_validator
        ]
    )
    total_usable_lamports = (
        distributable_lamports - num_increases * stake_rent_exemption
    )
    lamports_per_validator = total_usable_lamports // num_validators
    print(f"* {to_sol(lamports_per_validator)} desired per validator")

    changes = []
    for validator in validators:
        if validator.transient_stake_lamports != 0:
            print(
                f"Skipping {validator.vote_account_address}: "
                f"{to_sol(validator.transient_stake_lamports)} in transient stake"
            )
        else:
            if validator.active_stake_lamports > lamports_per_validator:
                lamports_to_decrease = (
                    validator.active_stake_lamports - lamports_per_validator
                )
                if lamports_to_decrease <= stake_rent_exemption:
                    print(f"Skipping decrease on {validator.vote_account_address}, \
currently at {to_sol(validator.active_stake_lamports)}, \
decrease of {to_sol(lamports_to_decrease)} below the rent exmption")
                else:
                    changes.append(
                        StakeChange(validator.vote_account_address, -lamports_to_decrease)
                    )
            elif validator.active_stake_lamports < lamports_per_validator:
                lamports_to_increase = (
                    lamports_per_validator - validator.active_stake_lamports
                )
                if lamports_to_increase < MINIMUM_ACTIVE_STAKE:
                    print(f"Skipping increase on {validator.vote_account_address}, \
currently at {to_sol(validator.active_stake_lamports)}, \
increase of {to_sol(lamports_to_increase)} less than the minimum of {to_sol(MINIMUM_ACTIVE_STAKE)}")
                else:
                    changes.append(
                        StakeChange(validator.vote_account_address, lamports_to_increase)
                    )
            else:
                print(
                    f"{validator.vote_account_address}: already at {to_sol(lamports_per_validator)}"
                )
    return changes


def compute_stake_level(
    external_stakes: List[int],
    pool_stakes: List[int],
    cap_lamports: int,
    budget_lamports: int,
    max_level: int,
) -> int:
    """Compute the highest total stake level the pool can bring every validator up to.

    A validator that already holds `external` lamports from other stakers needs
    `min(max(level - external, 0), cap_lamports)` from this pool to reach `level`, of which
    only what it is not staked yet, on top of `pool`, has to be paid for. The level is the
    highest one whose bill fits in `budget_lamports`, so the lowest staked validators are
    served first and the result is as even as the budget allows.
    """

    def bill(level: int) -> int:
        return sum(
            max(min(max(level - external, 0), cap_lamports) - pool, 0)
            for external, pool in zip(external_stakes, pool_stakes)
        )

    low, high = 0, max(max_level, 0)
    while low < high:
        middle = (low + high + 1) // 2
        if bill(middle) <= budget_lamports:
            low = middle
        else:
            high = middle - 1
    return low


def stake_from_pool(external_lamports: int, level: int, cap_lamports: int) -> int:
    """Lamports this pool has to stake to a validator to bring its total stake up to `level`."""
    return min(max(level - external_lamports, 0), cap_lamports)


def plan_equalized_stake(
    validators: List[ValidatorStakeInfo],
    activated_stakes: Dict[Pubkey, int],
    distributable_lamports: int,
    cap_lamports: int,
    usable_reserve_lamports: int,
    stake_rent_exemption: int,
) -> List[StakeChange]:
    """Plan a rebalance that makes the total stake of every validator as even as possible.

    What counts here is the stake a validator ends up voting with, `activated_stakes`, not
    only the part of it that comes from this pool: the distributable lamports go to the
    validators that are staked the least on the network first, so that voting rights end up
    spread as evenly as this pool can make them. The whole distributable amount is staked out
    even so, so once the least staked validators sit at their `cap_lamports`, what is left
    goes to validators that already vote with more than them. Validators holding transient
    stake or marked for removal cannot be touched in this epoch, so their stake is taken as
    given.

    Increases are additionally limited by `usable_reserve_lamports`, what the reserve can pay
    out right now, because stake that is being decreased only lands back in the reserve in the
    next epoch. Whatever cannot be staked stays in the reserve.
    """
    # a validator stake account cannot go below its rent exemption plus the minimum delegation
    minimum_validator_lamports = stake_rent_exemption + MINIMUM_ACTIVE_STAKE

    actionable: List[ValidatorStakeInfo] = []
    skipped: List[ValidatorStakeInfo] = []
    for validator in validators:
        if validator.status != StakeStatus.ACTIVE:
            print(f"Skipping {validator.vote_account_address}: marked for removal")
            skipped.append(validator)
        elif validator.transient_stake_lamports != 0:
            print(
                f"Skipping {validator.vote_account_address}: "
                f"{to_sol(validator.transient_stake_lamports)} in transient stake"
            )
            skipped.append(validator)
        else:
            actionable.append(validator)

    # the transient stake of a skipped validator either merges into its stake account or goes
    # back to the reserve in the next epoch, count it as staked to stay on the safe side of the cap
    untouchable_lamports = sum(
        validator.active_stake_lamports + validator.transient_stake_lamports
        for validator in skipped
    )

    print(f"* {len(actionable)} out of {len(validators)} validators can be rebalanced in this epoch")
    if not actionable:
        return []

    # what the validator votes with, minus what this pool gave it, is what it got elsewhere;
    # a stake account also holds a rent exemption that is not delegated, hence the clamp
    external_stakes = []
    for validator in actionable:
        if validator.vote_account_address not in activated_stakes:
            print(
                f"No vote account found for {validator.vote_account_address}, "
                f"assuming it is not staked by anyone else"
            )
        external_stakes.append(
            max(
                activated_stakes.get(validator.vote_account_address, 0)
                - validator.active_stake_lamports,
                0,
            )
        )
    pool_stakes = [validator.active_stake_lamports for validator in actionable]
    # the level only has to rise until every validator sits at its cap, the whole
    # distributable amount is put to work below that
    ceiling_level = max(external_stakes) + cap_lamports

    remaining_lamports = max(distributable_lamports - untouchable_lamports, 0)
    target_level = compute_stake_level(
        external_stakes,
        [0] * len(actionable),
        cap_lamports,
        remaining_lamports,
        ceiling_level,
    )
    target_stakes = [
        stake_from_pool(external, target_level, cap_lamports) for external in external_stakes
    ]
    print(
        f"* Levelling the total stake of every validator up to "
        f"{to_sol(max(external + target for external, target in zip(external_stakes, target_stakes)))}"
    )
    for validator, external, target in zip(actionable, external_stakes, target_stakes):
        print(
            f"  - {validator.vote_account_address}: total stake "
            f"{sols(external + validator.active_stake_lamports)} -> {to_sol(external + target)}, "
            f"of which {sols(validator.active_stake_lamports)} -> {to_sol(target)} from the pool"
        )
    if cap_lamports in target_stakes:
        print("* The per-validator cap is reached, the surplus is kept in the reserve")

    changes = []
    to_increase: List[int] = []
    for index, validator in enumerate(actionable):
        target = target_stakes[index]
        if validator.active_stake_lamports > target:
            lamports_to_decrease = validator.active_stake_lamports - max(
                target, minimum_validator_lamports
            )
            if lamports_to_decrease < MINIMUM_ACTIVE_STAKE:
                print(f"Skipping decrease on {validator.vote_account_address}, \
currently at {to_sol(validator.active_stake_lamports)}, \
decrease of {to_sol(lamports_to_decrease)} less than the minimum of {to_sol(MINIMUM_ACTIVE_STAKE)}")
            else:
                changes.append(
                    StakeChange(validator.vote_account_address, -lamports_to_decrease)
                )
        elif validator.active_stake_lamports < target:
            to_increase.append(index)
        else:
            print(f"{validator.vote_account_address}: already at {to_sol(target)}")

    # each increase parks a rent exemption in a new transient stake account until it merges
    # into the validator stake account, so the reserve has to cover it on top of the stake
    to_increase.sort(key=lambda index: external_stakes[index] + pool_stakes[index])
    while to_increase and usable_reserve_lamports < len(to_increase) * stake_rent_exemption:
        # the validators that are staked the most are the ones we give up on first
        dropped = actionable[to_increase.pop()]
        print(f"Skipping increase on {dropped.vote_account_address}, \
currently at {to_sol(dropped.active_stake_lamports)}, \
not enough left in the reserve")
    increase_budget = usable_reserve_lamports - len(to_increase) * stake_rent_exemption
    reachable_level = compute_stake_level(
        [external_stakes[index] for index in to_increase],
        [pool_stakes[index] for index in to_increase],
        cap_lamports,
        increase_budget,
        target_level,
    )
    if to_increase and reachable_level < target_level:
        print(f"* The reserve only covers a total stake of {to_sol(reachable_level)} per validator now")
    for index in to_increase:
        validator = actionable[index]
        lamports_to_increase = (
            stake_from_pool(external_stakes[index], reachable_level, cap_lamports)
            - validator.active_stake_lamports
        )
        if lamports_to_increase < MINIMUM_ACTIVE_STAKE:
            print(f"Skipping increase on {validator.vote_account_address}, \
currently at {to_sol(validator.active_stake_lamports)}, \
increase of {to_sol(lamports_to_increase)} less than the minimum of {to_sol(MINIMUM_ACTIVE_STAKE)}")
        else:
            changes.append(
                StakeChange(validator.vote_account_address, lamports_to_increase)
            )
    return changes


async def rebalance(
    endpoint: str,
    stake_pool_address: Pubkey,
    staker: Keypair,
    retained_reserve_amount: float,
    retained_reserve_percent: float,
    equalize_stake: bool = False,
    max_stake_percent: float = DEFAULT_MAX_STAKE_PERCENT,
    dry_run: bool = False,
):
    async_client = await get_client(endpoint)

    epoch_resp = await async_client.get_epoch_info(commitment=Confirmed)
    epoch = epoch_resp.value.epoch
    resp = await async_client.get_account_info(stake_pool_address, commitment=Confirmed)
    data = resp.value.data if resp.value else b""
    stake_pool = StakePool.decode(data)

    print(
        f"Stake pool last update epoch {stake_pool.last_update_epoch}, current epoch {epoch}"
    )
    if stake_pool.last_update_epoch != epoch:
        print("Updating stake pool")
        await update_stake_pool(async_client, staker, stake_pool_address)
        resp = await async_client.get_account_info(
            stake_pool_address, commitment=Confirmed
        )
        data = resp.value.data if resp.value else b""
        stake_pool = StakePool.decode(data)

    rent_resp = await async_client.get_minimum_balance_for_rent_exemption(STAKE_LEN)
    stake_rent_exemption = rent_resp.value

    val_resp = await async_client.get_account_info(
        stake_pool.validator_list, commitment=Confirmed
    )
    data = val_resp.value.data if val_resp.value else b""
    validator_list = ValidatorList.decode(data)

    retained_reserve_amount_lamports = int(retained_reserve_amount * LAMPORTS_PER_SOL)
    retained_reserve_percentage_lamports = int(
        retained_reserve_percent * stake_pool.total_lamports
    )
    retained_reserve_lamports = max(
        retained_reserve_amount_lamports, retained_reserve_percentage_lamports
    )
    retained_reserve_lamports_set_by = (
        "set by the reserve_amount option"
        if retained_reserve_amount_lamports > retained_reserve_percentage_lamports
        else "set by the reserve_percent option"
    )
    distributable_lamports = max(
        stake_pool.total_lamports - retained_reserve_lamports, 0
    )

    print("Stake pool stats:")
    print(f"* {to_sol(stake_pool.total_lamports)} in total")
    print(f"* {len(validator_list.validators)} validators")
    print(
        f"* Retaining {to_sol(retained_reserve_lamports)} in the reserve "
        f"({retained_reserve_lamports_set_by})"
    )
    print(f"* {to_sol(distributable_lamports)} to distribute over the validators")

    if equalize_stake:
        print("* Strategy: even total stake per validator, capped per validator")
        cap_lamports = int(max_stake_percent * stake_pool.total_lamports)
        print(
            f"* {to_sol(cap_lamports)} is the maximum stake per validator "
            f"({max_stake_percent:.1%} of the pool)"
        )
        # what every validator of the cluster votes with, this pool's stake included
        vote_resp = await async_client.get_vote_accounts(commitment=Confirmed)
        activated_stakes = {
            vote_account.vote_pubkey: vote_account.activated_stake
            for vote_account in list(vote_resp.value.current)
            + list(vote_resp.value.delinquent)
        }
        reserve_resp = await async_client.get_balance(
            stake_pool.reserve_stake, commitment=Confirmed
        )
        # the reserve must stay rent exempt and hold on to the retained amount, only
        # what is left on top of that can be staked out in this epoch
        usable_reserve_lamports = max(
            reserve_resp.value
            - stake_rent_exemption
            - MINIMUM_RESERVE_LAMPORTS
            - retained_reserve_lamports,
            0,
        )
        print(f"* {to_sol(usable_reserve_lamports)} of the reserve can be staked out now")
        changes = plan_equalized_stake(
            validator_list.validators,
            activated_stakes,
            distributable_lamports,
            cap_lamports,
            usable_reserve_lamports,
            stake_rent_exemption,
        )
    else:
        print("* Strategy: even split over all the validators")
        changes = plan_even_split(
            validator_list.validators,
            distributable_lamports,
            stake_rent_exemption,
        )

    print(f"Planned changes on {len(changes)} validators:")
    for change in changes:
        action = "Increase" if change.lamports > 0 else "Decrease"
        print(
            f"* {action} {change.vote_account_address} by {to_sol(abs(change.lamports))}"
        )

    if dry_run:
        print("Dry-run mode enabled, the execution is skipped")
    else:
        print("Executing strategy")
        futures = []
        for change in changes:
            if change.lamports > 0:
                futures.append(
                    increase_validator_stake(
                        async_client,
                        staker,
                        staker,
                        stake_pool_address,
                        change.vote_account_address,
                        change.lamports,
                    )
                )
            else:
                futures.append(
                    decrease_validator_stake(
                        async_client,
                        staker,
                        staker,
                        stake_pool_address,
                        change.vote_account_address,
                        -change.lamports,
                    )
                )
        await asyncio.gather(*futures)
        print("Done")
    await async_client.close()


def keypair_from_file(keyfile_name: str) -> Keypair:
    with open(keyfile_name, "r") as keyfile:
        data = keyfile.read()
    return Keypair.from_json(data)


async def get_epoch_progress(async_client: AsyncClient) -> tuple[int, float]:
    epoch_resp = await async_client.get_epoch_info(commitment=Confirmed)
    epoch_info = epoch_resp.value
    progress = epoch_info.slot_index / epoch_info.slots_in_epoch
    return epoch_info.epoch, progress


async def service_mode(
    endpoint: str,
    stake_pool_address: Pubkey,
    staker: Keypair,
    reserve_amount: float,
    reserve_percent: float,
    equalize_stake: bool = False,
    max_stake_percent: float = DEFAULT_MAX_STAKE_PERCENT,
    dry_run: bool = False,
):
    async_client = await get_client(endpoint)
    rebalanced_in_current_epoch = False
    print("Starting service mode - monitoring epoch progress...")

    while True:
        try:
            epoch, progress = await get_epoch_progress(async_client)

            print(f"Current epoch: {epoch}, progress: {progress:.2%}")

            resp = await async_client.get_account_info(
                stake_pool_address, commitment=Confirmed
            )
            data = resp.value.data if resp.value else b""
            stake_pool = StakePool.decode(data)

            print(
                f"Stake pool last update epoch {stake_pool.last_update_epoch}, current epoch {epoch}"
            )

            if stake_pool.last_update_epoch != epoch:
                print(f"The pool has not been updated for epoch {epoch} yet")
                print(f"Updating stake pool for epoch {epoch}")
                try:
                    await update_stake_pool(async_client, staker, stake_pool_address)
                    print(f"Updating stake pool for epoch {epoch} done")
                except Exception as e:
                    print(f"Error when updating the pool: {e}")
                    # one of the potential reasons is there are some transient stake accounts
                    # in an unexpected state for example, the total amount of stakes to be
                    # activated hits the epoch warming up limit and it takes more epochs to
                    # become fully active
                    # in this case, we retry to update the pool without merging the stake accounts
                    # ref: https://docs.anza.xyz/consensus/stake-delegation-and-rewards#stake-warmup-cooldown-withdrawal
                    await update_stake_pool(
                        async_client, staker, stake_pool_address, True
                    )
                    print(f"Updating stake pool without merges for epoch {epoch} done")
                rebalanced_in_current_epoch = False

            if progress >= 0.95 and not rebalanced_in_current_epoch:
                print(f"Epoch {epoch} is {progress:.2%} complete - starting rebalance")
                await rebalance(
                    endpoint,
                    stake_pool_address,
                    staker,
                    reserve_amount,
                    reserve_percent,
                    equalize_stake,
                    max_stake_percent,
                    dry_run,
                )
                rebalanced_in_current_epoch = True
                print(f"Rebalance completed for epoch {epoch}")

            await asyncio.sleep(30)

        except Exception as e:
            print(f"Error in service mode: {e}")
            await asyncio.sleep(60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Rebalance stake evenly between all the validators in a stake pool."
    )
    parser.add_argument(
        "stake_pool",
        metavar="STAKE_POOL_ADDRESS",
        type=str,
        help="Stake pool to rebalance, given by a public key in base-58,\
                         e.g. Zg5YBPAk8RqBR9kaLLSoN5C8Uv7nErBz1WC63HTsCPR",
    )
    parser.add_argument(
        "staker",
        metavar="STAKER_KEYPAIR",
        type=str,
        help="Staker for the stake pool, given by a keypair file, e.g. staker.json",
    )
    parser.add_argument(
        "--reserve_amount",
        metavar="RESERVE_AMOUNT",
        type=float,
        default=0,
        help="Amount of SOL to keep in the reserve, e.g. 10.5",
    )
    parser.add_argument(
        "--reserve_percent",
        metavar="RESERVE_PERCENT",
        type=float,
        default=0,
        help="Percentage of the total SOLs in the pool to keep in the reserve, e.g. 0.1 (means 10%%). \
            Note that the bot reserves max(reserve_amount, reserve_percent * total).",
    )
    parser.add_argument(
        "--equalize_stake",
        action="store_true",
        help="Distribute the lamports left after the reserve so that the total stake of every \
            validator, what it got from this pool and from every other staker, ends up as even \
            as possible, capped by max_stake_percent, instead of splitting them evenly over \
            all the validators.",
    )
    parser.add_argument(
        "--max_stake_percent",
        metavar="MAX_STAKE_PERCENT",
        type=float,
        default=None,
        help=f"Percentage of the total SOLs in the pool to stake to a single validator at most, \
            e.g. 0.05 (means 5%%). Only used together with equalize_stake, \
            defaults to {DEFAULT_MAX_STAKE_PERCENT} (means {DEFAULT_MAX_STAKE_PERCENT * 100:.0f}%%).",
    )
    parser.add_argument(
        "--endpoint",
        metavar="ENDPOINT_URL",
        type=str,
        default="https://api.mainnet-beta.solana.com",
        help="RPC endpoint to use, e.g. https://api.mainnet-beta.solana.com",
    )
    parser.add_argument(
        "--service",
        action="store_true",
        help="Run in service mode with epoch-based periodic rebalancing",
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        help="Dry run mode that skips the actual executions",
    )

    args = parser.parse_args()
    if args.max_stake_percent is None:
        max_stake_percent = DEFAULT_MAX_STAKE_PERCENT
    elif not args.equalize_stake:
        parser.error("max_stake_percent requires equalize_stake")
    elif not 0 < args.max_stake_percent <= 1:
        parser.error("max_stake_percent must be in (0, 1]")
    else:
        max_stake_percent = args.max_stake_percent

    stake_pool = Pubkey.from_string(args.stake_pool)
    staker = keypair_from_file(args.staker)
    print(f"Stake pool: {stake_pool}")
    print(f"Staker public key: {staker.pubkey()}")
    print(
        f"Amount to leave in the reserve: max({args.reserve_amount} SOL, {args.reserve_percent:.1%} of the total)"
    )
    if args.equalize_stake:
        print(
            f"Distributing the rest by equalizing the total stake of every validator, at most "
            f"{max_stake_percent:.1%} of the pool to a single validator"
        )
    else:
        print("Distributing the rest evenly over all the validators")

    if args.service:
        print("Running in service mode")
        asyncio.run(
            service_mode(
                args.endpoint,
                stake_pool,
                staker,
                args.reserve_amount,
                args.reserve_percent,
                args.equalize_stake,
                max_stake_percent,
                args.dry_run,
            )
        )
    else:
        print("Running one-time rebalance")
        asyncio.run(
            rebalance(
                args.endpoint,
                stake_pool,
                staker,
                args.reserve_amount,
                args.reserve_percent,
                args.equalize_stake,
                max_stake_percent,
                args.dry_run,
            )
        )
