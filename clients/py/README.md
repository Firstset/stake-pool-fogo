# Stake-Pool Python Bindings

Preliminary Python bindings to interact with the stake pool program, enabling
simple stake delegation bots.

## To do

* More reference bot implementations
* Add bindings for all stake pool instructions, see `TODO`s in `stake_pool/instructions.py`
* Finish bindings for vote and stake program
* Upstream vote and stake program bindings to https://github.com/michaelhly/solana-py

## Development

### Environment Setup

1. Ensure that Python 3 is installed with `venv`: https://www.python.org/downloads/
2. (Optional, but highly recommended) Setup and activate a virtual environment:

```
$ python3 -m venv venv
$ source venv/bin/activate
```

3. Install build and dev requirements

```
$ pip install -r requirements.txt
$ pip install -r optional-requirements.txt
```

4. Install the Solana tool suite: https://docs.solana.com/cli/install-solana-cli-tools

### Test

Testing through `pytest`:

```
$ python3 -m pytest
```

Note: the tests all run against a `solana-test-validator` with short epochs of 64
slots (25.6 seconds exactly). Some tests wait for epoch changes, so they take
time, roughly 90 seconds total at the time of this writing.

### Formatting

```
$ flake8 bot spl_token stake stake_pool system tests vote
```

### Type Checker

```
$ mypy bot stake stake_pool tests vote spl_token system
```

## Delegation Bots

The `./bot` directory contains sample stake pool delegation bot implementations:

* `rebalance`: simple bot to make the amount delegated to each validator
uniform, while also maintaining some SOL in the reserve if desired. Can be run
with the stake pool address, staker keypair, and SOL to leave in the reserve:

```
$ python3 bot/rebalance.py Zg5YBPAk8RqBR9kaLLSoN5C8Uv7nErBz1WC63HTsCPR staker.json 10.5
```

The amount to keep in the reserve, `max(--reserve_amount, --reserve_percent * total)`,
is always taken out first. What is left over is then distributed over the validators by
one of two strategies:

* by default, it is split evenly over all the validators.
* with `--equalize_stake`, it is distributed so that the *total* stake of every
validator ends up as even as possible, without ever staking more than
`--max_stake_percent` of the pool to a single validator (defaults to 0.33, meaning 33%).

What the second strategy evens out is the stake a validator votes with, read from
`getVoteAccounts`, not only the part of it that comes from this pool: a validator that
is already staked by others gets less from the pool, and the least staked validators are
served first, so that voting rights end up as evenly spread as this pool can make them.
The whole distributable amount is staked out even so, so once the least staked
validators sit at their cap, what is left goes to validators that already vote with more
than them; only what no validator can take under the cap stays in the reserve.

The strategy also takes what cannot be moved into account: validators holding transient
stake or marked for removal cannot be touched in the current epoch, so their stake is
taken as given and the distributable lamports are spread over the remaining ones.
Increases are limited by what the reserve can pay out at that moment, since stake that
is being decreased only lands back in the reserve in the next epoch, so it can take a
few epochs to converge.

```
$ python3 bot/rebalance.py Zg5YBPAk8RqBR9kaLLSoN5C8Uv7nErBz1WC63HTsCPR staker.json \
    --reserve_percent 0.1 --equalize_stake --max_stake_percent 0.05
```
