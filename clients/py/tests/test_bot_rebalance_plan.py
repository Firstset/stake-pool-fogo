"""Unit tests for the rebalance planners, no test validator needed."""

from typing import Dict, List

from bot.rebalance import (
    StakeChange,
    compute_stake_level,
    plan_equalized_stake,
    plan_even_split,
)
from solders.pubkey import Pubkey
from stake_pool.constants import MINIMUM_ACTIVE_STAKE
from stake_pool.state import StakeStatus, ValidatorStakeInfo

STAKE_RENT_EXEMPTION: int = 2_282_880

# every stake movement must be at least the minimum delegation, so the amounts in
# these tests are expressed as multiples of it
UNIT: int = MINIMUM_ACTIVE_STAKE

# a cap that is high enough to never be reached
NO_CAP: int = 1_000 * UNIT


def vote_account(index: int) -> Pubkey:
    return Pubkey([index] + [0] * 31)


def validator(
    index: int,
    active_stake_lamports: int,
    transient_stake_lamports: int = 0,
    status: StakeStatus = StakeStatus.ACTIVE,
) -> ValidatorStakeInfo:
    return ValidatorStakeInfo(
        active_stake_lamports=active_stake_lamports,
        transient_stake_lamports=transient_stake_lamports,
        last_update_epoch=0,
        transient_seed_suffix=0,
        unused=0,
        validator_seed_suffix=0,
        status=status,
        vote_account_address=vote_account(index),
    )


def activated_stakes(*totals: int) -> Dict[Pubkey, int]:
    """Total stake every validator votes with, given in the order of the validator list."""
    return {vote_account(index + 1): total for index, total in enumerate(totals)}


def no_external_stake(validators: List[ValidatorStakeInfo]) -> Dict[Pubkey, int]:
    """Total stakes for validators that only ever got stake from this pool."""
    return {
        validator.vote_account_address: validator.active_stake_lamports
        for validator in validators
    }


def changes_by_index(changes: List[StakeChange]) -> Dict[int, int]:
    return {bytes(change.vote_account_address)[0]: change.lamports for change in changes}


def test_compute_stake_level_serves_the_least_staked_validators_first():
    # 30 lamports bring the two least staked validators up to the third one
    assert compute_stake_level([100, 110, 120], [0, 0, 0], 1000, 30, 1000) == 120
    # anything left over is spread over all of them
    assert compute_stake_level([100, 110, 120], [0, 0, 0], 1000, 60, 1000) == 130
    # a partial budget stops in between, here 10 lamports only lift the lowest one
    assert compute_stake_level([100, 110, 120], [0, 0, 0], 1000, 10, 1000) == 110
    # what the pool already staked does not have to be paid for again
    assert compute_stake_level([100, 110, 120], [20, 0, 0], 1000, 10, 1000) == 120
    # the per validator cap limits how much the pool tops a validator up with
    assert compute_stake_level([100, 110, 120], [0, 0, 0], 5, 7, 1000) == 112
    # and so does the maximum level
    assert compute_stake_level([100, 110, 120], [0, 0, 0], 1000, 1000, 115) == 115


def test_equalized_stake_evens_out_the_total_stake_of_every_validator():
    validators = [validator(1, 1 * UNIT), validator(2, 1 * UNIT), validator(3, 1 * UNIT)]
    # the second validator already votes with 3 units more than the others
    changes = plan_equalized_stake(
        validators,
        activated_stakes(1 * UNIT, 4 * UNIT, 1 * UNIT),
        distributable_lamports=9 * UNIT,
        cap_lamports=NO_CAP,
        usable_reserve_lamports=6 * UNIT,
        stake_rent_exemption=0,
    )
    # every validator ends up voting with 4 units: the pool tops up the other two
    assert changes_by_index(changes) == {1: 3 * UNIT, 3: 3 * UNIT}


def test_equalized_stake_pulls_stake_away_from_the_most_staked_validator():
    validators = [validator(1, 5 * UNIT), validator(2, 1 * UNIT)]
    # the first validator votes with 10 units, 5 of which come from this pool
    changes = plan_equalized_stake(
        validators,
        activated_stakes(10 * UNIT, 1 * UNIT),
        distributable_lamports=6 * UNIT,
        cap_lamports=NO_CAP,
        usable_reserve_lamports=1 * UNIT,
        stake_rent_exemption=0,
    )
    # both should end up voting with 5.5 units, so the pool takes back what it can from
    # the first one, down to the minimum delegation it has to leave there, and gives the
    # second one everything the reserve can pay out right now
    assert changes_by_index(changes) == {1: -4 * UNIT, 2: 1 * UNIT}


def test_equalized_stake_falls_back_to_an_even_split_without_external_stake():
    validators = [validator(1, 3 * UNIT), validator(2, 1 * UNIT), validator(3, 2 * UNIT)]
    changes = plan_equalized_stake(
        validators,
        no_external_stake(validators),
        distributable_lamports=12 * UNIT,
        cap_lamports=NO_CAP,
        usable_reserve_lamports=6 * UNIT,
        stake_rent_exemption=0,
    )
    # nothing but the pool stakes these validators, so they all end up at 12 / 3 units
    assert changes_by_index(changes) == {1: 1 * UNIT, 2: 3 * UNIT, 3: 2 * UNIT}


def test_equalized_stake_leaves_the_retained_reserve_alone():
    validators = [validator(1, 1 * UNIT), validator(2, 1 * UNIT)]
    # 10 units in the pool, of which 4 are retained in the reserve: only 6 are
    # distributed, and only 4 of the 6 units sitting in the reserve can be staked out
    changes = plan_equalized_stake(
        validators,
        no_external_stake(validators),
        distributable_lamports=6 * UNIT,
        cap_lamports=NO_CAP,
        usable_reserve_lamports=4 * UNIT,
        stake_rent_exemption=0,
    )
    assert changes_by_index(changes) == {1: 2 * UNIT, 2: 2 * UNIT}


def test_equalized_stake_respects_the_per_validator_cap():
    validators = [validator(1, 5 * UNIT), validator(2, 1 * UNIT), validator(3, 1 * UNIT)]
    changes = plan_equalized_stake(
        validators,
        no_external_stake(validators),
        distributable_lamports=12 * UNIT,
        cap_lamports=3 * UNIT,
        usable_reserve_lamports=5 * UNIT,
        stake_rent_exemption=0,
    )
    # an even split would be 4 units each, the cap allows only 3
    assert changes_by_index(changes) == {1: -2 * UNIT, 2: 2 * UNIT, 3: 2 * UNIT}


def test_equalized_stake_stakes_everything_out_once_the_cap_is_reached():
    validators = [validator(1, 1 * UNIT), validator(2, 1 * UNIT)]
    # the first validator votes with nothing but the pool stake, the second one is
    # already staked with 9 units by others
    changes = plan_equalized_stake(
        validators,
        activated_stakes(1 * UNIT, 10 * UNIT),
        distributable_lamports=10 * UNIT,
        cap_lamports=4 * UNIT,
        usable_reserve_lamports=8 * UNIT,
        stake_rent_exemption=0,
    )
    # evening out the totals would ask for 8 units on the first validator, the cap allows
    # 4, and the rest is staked to the second one rather than left idle in the reserve
    assert changes_by_index(changes) == {1: 3 * UNIT, 2: 3 * UNIT}


def test_equalized_stake_keeps_untouchable_validators_in_the_average():
    validators = [
        validator(1, 6 * UNIT, transient_stake_lamports=UNIT // 2),
        validator(2, 1 * UNIT),
        validator(3, 1 * UNIT),
    ]
    changes = plan_equalized_stake(
        validators,
        no_external_stake(validators),
        distributable_lamports=12 * UNIT + UNIT // 2,
        cap_lamports=NO_CAP,
        usable_reserve_lamports=4 * UNIT,
        stake_rent_exemption=0,
    )
    # the first validator is untouchable at 6.5 units, the remaining 6 are split evenly
    assert changes_by_index(changes) == {2: 2 * UNIT, 3: 2 * UNIT}


def test_equalized_stake_skips_validators_marked_for_removal():
    validators = [
        validator(1, 3 * UNIT, status=StakeStatus.READY_FOR_REMOVAL),
        validator(2, 1 * UNIT),
        validator(3, 1 * UNIT),
    ]
    changes = plan_equalized_stake(
        validators,
        no_external_stake(validators),
        distributable_lamports=8 * UNIT,
        cap_lamports=NO_CAP,
        usable_reserve_lamports=3 * UNIT,
        stake_rent_exemption=0,
    )
    # the 5 units that are not stuck on the first validator are split over the other two
    assert changes_by_index(changes) == {2: UNIT + UNIT // 2, 3: UNIT + UNIT // 2}


def test_equalized_stake_limits_the_increases_to_the_reserve_balance():
    validators = [validator(1, 1 * UNIT), validator(2, 2 * UNIT), validator(3, 9 * UNIT)]
    # the target is 5 units each, but the reserve only holds 3, and what is decreased
    # on the third validator only lands back in the reserve in the next epoch
    changes = plan_equalized_stake(
        validators,
        no_external_stake(validators),
        distributable_lamports=15 * UNIT,
        cap_lamports=NO_CAP,
        usable_reserve_lamports=3 * UNIT,
        stake_rent_exemption=0,
    )
    # the two lowest validators are levelled out at 3 units with what the reserve has
    assert changes_by_index(changes) == {1: 2 * UNIT, 2: 1 * UNIT, 3: -4 * UNIT}


def test_equalized_stake_reserves_the_rent_exemption_of_every_increase():
    validators = [validator(1, 1 * UNIT), validator(2, 1 * UNIT)]
    usable_reserve_lamports = 2 * UNIT + 2 * STAKE_RENT_EXEMPTION
    changes = plan_equalized_stake(
        validators,
        no_external_stake(validators),
        distributable_lamports=2 * UNIT + usable_reserve_lamports,
        cap_lamports=NO_CAP,
        usable_reserve_lamports=usable_reserve_lamports,
        stake_rent_exemption=STAKE_RENT_EXEMPTION,
    )
    # only what is left after funding both transient stake accounts is staked out
    assert changes_by_index(changes) == {1: 1 * UNIT, 2: 1 * UNIT}


def test_equalized_stake_skips_moves_below_the_minimum_delegation():
    validators = [validator(1, 10 * UNIT), validator(2, 10 * UNIT - 1)]
    changes = plan_equalized_stake(
        validators,
        no_external_stake(validators),
        distributable_lamports=20 * UNIT - 1,
        cap_lamports=NO_CAP,
        usable_reserve_lamports=10 * UNIT,
        stake_rent_exemption=STAKE_RENT_EXEMPTION,
    )
    # a single lamport apart is not worth a transaction
    assert changes == []


def test_equalized_stake_does_nothing_when_no_validator_is_actionable():
    validators = [validator(1, UNIT, transient_stake_lamports=UNIT)]
    assert plan_equalized_stake(validators, {}, 2 * UNIT, NO_CAP, 0, 0) == []


def test_even_split_spreads_the_distributable_lamports_over_all_validators():
    validators = [validator(1, 3 * UNIT), validator(2, 1 * UNIT)]
    # 10 units in the pool with 2 units retained in the reserve leaves 8 to split
    changes = plan_even_split(
        validators,
        distributable_lamports=8 * UNIT,
        stake_rent_exemption=0,
    )
    assert changes_by_index(changes) == {1: 1 * UNIT, 2: 3 * UNIT}
