"""Every cell reachable by name, and the four catalogues shaped alike.

Cell A had no catalogue until Sprint 109: it was reachable only through
the atheris and Hypothesis harnesses, which *generate* inputs rather than
name them.  That is a difference in kind, not degree — a generated input
says something broke, a named case says which known mistake is being
tested, and only the second can be cited in a report or parametrized over
by someone else's suite.

The shape checks here are the cheap half of the role-axis guard: they run
without Docker, without a network peer, and catch a cell drifting from the
other three at import time.  The expensive half — actually driving each
cell at a non-BlackBull counterpart — lives in
`tests/conformance/fault_injection/test_four_cell_differential.py`.
"""
from __future__ import annotations

import pytest


class TestTheGridIsComplete:
    def test_all_four_cells_have_a_named_catalogue(self):
        from blackbull.fault_injection.catalogue import CATALOGUES

        assert set(CATALOGUES) == {'h1_client', 'h1_server',
                                   'h2_client', 'h2_server'}
        empty = [cell for cell, cases in CATALOGUES.items() if not cases]
        assert empty == [], f'cells with no named cases: {empty}'

    def test_every_case_builds_without_io_and_is_repeatable(self):
        """Builders are pure: two calls give equal scenarios.

        The catalogue's contract, and what lets a suite build a case per
        parametrized run without the runs interfering.
        """
        from blackbull.fault_injection.catalogue import CATALOGUES

        for cell, cases in CATALOGUES.items():
            for name, build in cases.items():
                first, second = build(), build()
                assert first == second, f'{cell}.{name} is not repeatable'

    def test_every_case_is_named_after_its_key(self):
        """A result names the case; a mismatch makes a report point wrong."""
        from blackbull.fault_injection.catalogue import CATALOGUES

        wrong = [f'{cell}.{key} -> {build().name!r}'
                 for cell, cases in CATALOGUES.items()
                 for key, build in cases.items()
                 if getattr(build(), 'name', key) != key]
        assert wrong == [], wrong

    def test_every_case_round_trips_through_json(self):
        """A scenario is data — the property the whole toolkit rests on."""
        from blackbull.fault_injection import (
            scenario_h1_server_from_json, scenario_h1_server_to_json,
            scenario_h2_client_from_json, scenario_h2_client_to_json,
            scenario_h2_from_json, scenario_h2_to_json,
        )
        from blackbull.fault_injection.scenario_h1 import (
            scenario_from_json as h1c_from_json,
            scenario_to_json as h1c_to_json,
        )
        from blackbull.fault_injection.catalogue import CATALOGUES

        codecs = {
            'h1_client': (h1c_to_json, h1c_from_json),
            'h1_server': (scenario_h1_server_to_json,
                          scenario_h1_server_from_json),
            'h2_client': (scenario_h2_client_to_json,
                          scenario_h2_client_from_json),
            'h2_server': (scenario_h2_to_json, scenario_h2_from_json),
        }
        for cell, cases in CATALOGUES.items():
            to_json, from_json = codecs[cell]
            for name, build in cases.items():
                original = build()
                assert from_json(to_json(original)) == original, \
                    f'{cell}.{name} did not survive a JSON round trip'


class TestTheVocabulariesStayInStep:
    """The role axis, guarded at import time.

    Sprint 108 checked the protocol axis (h1 ↔ h2) and found it symmetric;
    the role axis (client ↔ server) had never been checked, and that is
    where both of that week's defects lived.  This is the guard for it.
    """

    def test_the_two_roles_report_the_same_things_by_the_same_names(self):
        from blackbull.fault_injection.scenario_h1 import ScenarioResult
        from blackbull.fault_injection.scenario_h1_server import (
            ScenarioH1ServerResult)
        from blackbull.fault_injection.scenario_h2 import ScenarioH2Result
        from blackbull.fault_injection.scenario_h2_client import (
            ScenarioH2ClientResult)

        shared = {'elapsed_s', 'exception', 'expectations', 'half_closed',
                  'steps_completed', 'wait_skipped', 'wait_timed_out'}
        for cls in (ScenarioResult, ScenarioH2ClientResult,
                    ScenarioH1ServerResult, ScenarioH2Result):
            missing = shared - set(cls.__dataclass_fields__)
            assert not missing, f'{cls.__name__} lacks {sorted(missing)}'

    @pytest.mark.parametrize('module_name,steps', [
        ('scenario_h1', ('WaitForResponse', 'ExpectResponse', 'HalfClose',
                         'ReadResponse', 'Abort', 'Sleep', 'SendRawBytes')),
        ('scenario_h2_client', ('WaitForServerFrame', 'ExpectServerFrame',
                                'HalfClose', 'ReadResponse', 'Abort', 'Sleep',
                                'SendRawBytes')),
        ('scenario_h1_server', ('WaitForRequest', 'ExpectRequest', 'HalfClose',
                                'Abort', 'Sleep', 'SendRawBytes')),
        ('scenario_h2', ('WaitForClientFrame', 'ExpectClientFrame',
                         'HalfClose', 'Abort', 'Sleep', 'SendRawBytes')),
    ])
    def test_each_vocabulary_carries_the_whole_scheme(self, module_name, steps):
        import importlib

        mod = importlib.import_module(f'blackbull.fault_injection.{module_name}')
        missing = [s for s in steps if not hasattr(mod, s)]
        assert missing == [], f'{module_name} lacks {missing}'


class TestTheDocsImportsResolve:
    """Import exactly as `docs/guide/fault_injection.md` tells a reader to.

    Sprint 108 shipped a documented quick-start whose `SendRawBytes`
    resolved to the wrong cell's step, and the failure surfaced only when
    someone ran the page.  A package export is part of the API; a name the
    docs use and the package does not export is a broken page, so the
    check belongs next to the code rather than in a reader's terminal.
    """

    def test_every_step_the_guide_names_is_importable(self):
        import blackbull.fault_injection as fi

        # Names appearing in a code block in the guide.
        for name in ('H2CSendFrame', 'H2CSendPreface', 'ScenarioH2Client',
                     'WaitForServerFrame', 'ExpectServerFrame',
                     'WaitForResponse', 'ExpectResponse', 'HalfClose',
                     'H1SSendRawBytes', 'H2CSendRawBytes', 'SendRawBytes'):
            assert hasattr(fi, name), f'the guide names {name}; the package does not export it'

    def test_the_role_qualified_aliases_point_at_the_right_cell(self):
        from blackbull import fault_injection as fi
        from blackbull.fault_injection import (
            scenario_h1, scenario_h2_client,
        )

        assert fi.H1CWaitForResponse is scenario_h1.WaitForResponse
        assert fi.H2CWaitForServerFrame is scenario_h2_client.WaitForServerFrame
        assert fi.WaitForResponse is scenario_h1.WaitForResponse
        assert fi.WaitForServerFrame is scenario_h2_client.WaitForServerFrame
