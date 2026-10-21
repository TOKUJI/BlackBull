"""One row per (cap, protocol): exceed the cap, then check the answer.

A row's *provoke* drives a real server or client past the cap and returns what
the protocol answered, as a short string ('431', 'RST_STREAM
ENHANCE_YOUR_CALM', 'ResponseTooLarge', ...).  The table test asserts that
answer, that the refusal was recorded under the cap's name and protocol, and,
where the cap is a setting, that the setting's documentation states the
answer — which is what "answers as documented" means here.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable

import pytest

from blackbull.env import Settings, reset_settings_cache

ROW_TIMEOUT = 15.0
_DOCS = {f.name: f.metadata['doc'] for f in dataclasses.fields(Settings)}


@dataclasses.dataclass(frozen=True)
class Row:
    cap: str
    protocol: str | None
    answer: str
    provoke: Callable[[], Awaitable[str]]
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    #: The Settings field whose description must state *answer*; ``None`` for
    #: a cap that is not a setting.
    setting: str | None = None
    #: Text the description states, when it words the answer differently.
    documented: str | None = None
    #: The issue tracking a known wrong answer; the row then must fail.
    xfail: str | None = None
    #: Traffic that stays within the cap, which must leave no record, and
    #: the answer it gets (so a control cannot pass by failing early).
    under: Callable[[], Awaitable[str]] | None = None
    under_answer: str | None = None


def table(*rows: Row, under: bool = False):
    """Parametrize over *rows*; with *under*, over those with an ``under`` control."""
    if under:
        return pytest.mark.parametrize('row', [
            pytest.param(r, id=f'{r.cap}-{r.protocol or "none"}') for r in rows if r.under])
    return pytest.mark.parametrize('row', [
        pytest.param(r, id=f'{r.cap}-{r.protocol or "none"}', marks=(
            [pytest.mark.xfail(strict=True, reason=r.xfail)] if r.xfail else []))
        for r in rows])


async def check(row: Row, monkeypatch, caplog) -> None:
    for name, value in row.env.items():
        monkeypatch.setenv(name, value)
    reset_settings_cache()
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    answer = await asyncio.wait_for(row.provoke(), ROW_TIMEOUT)
    assert answer == row.answer
    records = [r for r in caplog.records
               if r.name == 'blackbull.caps' and getattr(r, 'cap', None) == row.cap]
    assert records, f'{row.cap} refused without a record'
    assert records[0].protocol == row.protocol
    if row.setting is not None:
        stated = row.documented or row.answer
        assert stated in _DOCS[row.setting], (
            f'the {row.setting} documentation does not state {stated!r}')


async def check_under(row: Row, monkeypatch, caplog) -> None:
    for name, value in row.env.items():
        monkeypatch.setenv(name, value)
    reset_settings_cache()
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    assert await asyncio.wait_for(row.under(), ROW_TIMEOUT) == row.under_answer
    assert not [r for r in caplog.records if getattr(r, 'cap', None) == row.cap], (
        f'{row.cap} recorded for traffic within the cap')


def rows_of(*modules) -> list[Row]:
    return [row for module in modules for row in module.ROWS]
