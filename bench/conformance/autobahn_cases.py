"""Resolve and verify Autobahn heavy-lane case manifests.

The ``resolve`` command runs under the pinned tester's Python 2 runtime and
uses its CaseSet without altering test execution. The remaining commands run
under the host Python and validate the boundary between isolated batches.
"""

from __future__ import print_function

import json
import sys


EXPECTED_GROUPS = [
    '12.1', '12.2', '12.3', '12.4', '12.5',
    '13.1', '13.2', '13.3', '13.4', '13.5', '13.6', '13.7',
]
EXPECTED_CASE_COUNT = 156
ACCEPTED_BEHAVIOR = {'OK', 'NON-STRICT', 'INFORMATIONAL'}
ACCEPTED_CLOSE = {'OK', 'INFORMATIONAL'}

try:
    STRING_TYPES = (basestring,)  # noqa: F821 - defined by the tester's Python 2
except NameError:
    STRING_TYPES = (str,)


def _fail(message):
    print('ERROR: {0}'.format(message), file=sys.stderr)
    return 1


def _load_json(path, reject_duplicate_keys=False):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON object key: {0}'.format(key))
            result[key] = value
        return result

    with open(path) as source:
        if reject_duplicate_keys:
            return json.load(source, object_pairs_hook=unique_object)
        return json.load(source)


def _write_json(path, value):
    with open(path, 'w') as destination:
        json.dump(value, destination, indent=2)
        destination.write('\n')


def resolve(selector, output_path):
    sys.path.insert(0, '/opt/pypy/site-packages/autobahntestsuite')
    from case import (  # pylint: disable=import-outside-toplevel
        CaseBasename,
        CaseCategories,
        Cases,
        CaseSetname,
        CaseSubCategories,
    )
    from caseset import CaseSet  # pylint: disable=import-outside-toplevel

    patterns = [part.strip() for part in selector.split(',') if part.strip()]
    if not patterns:
        return _fail('case selector is empty')
    case_set = CaseSet(
        CaseSetname,
        CaseBasename,
        Cases,
        CaseCategories,
        CaseSubCategories,
    )
    resolved = case_set.parseSpecCases({
        'cases': patterns,
        'exclude-cases': [],
    })
    _write_json(output_path, resolved)
    return 0


def build_manifest(resolved_path, manifest_path):
    resolved = _load_json(resolved_path)
    if not isinstance(resolved, list) or not resolved:
        return _fail('resolver returned an empty or non-list manifest')
    if not all(isinstance(case_id, STRING_TYPES) for case_id in resolved):
        return _fail('resolver returned a non-string case ID')
    if len(resolved) != len(set(resolved)):
        return _fail('resolver returned duplicate case IDs')
    if len(resolved) != EXPECTED_CASE_COUNT:
        return _fail(
            'resolver returned {0} cases, expected {1}'.format(
                len(resolved), EXPECTED_CASE_COUNT,
            )
        )

    grouped = {}
    for case_id in resolved:
        parts = case_id.split('.')
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return _fail('invalid resolved case ID: {0}'.format(case_id))
        group = '.'.join(parts[:2])
        grouped.setdefault(group, []).append(case_id)

    if sorted(grouped) != sorted(EXPECTED_GROUPS):
        return _fail(
            'unexpected subgroup set: {0}'.format(sorted(grouped))
        )
    batches = [
        {'name': group, 'cases': grouped[group]}
        for group in EXPECTED_GROUPS
    ]
    flattened = [
        case_id
        for batch in batches
        for case_id in batch['cases']
    ]
    if len(flattened) != len(set(flattened)):
        return _fail('subgroups overlap')
    if set(flattened) != set(resolved):
        return _fail('subgroup union differs from the resolved case set')
    if flattened != resolved:
        return _fail('subgroups change the tester-resolved case order')

    _write_json(manifest_path, {
        'resolved_count': len(resolved),
        'resolved_unique_count': len(set(resolved)),
        'batch_count': len(batches),
        'batches': batches,
    })
    return 0


def verify_report(expected_path, index_path):
    expected = _load_json(expected_path)
    if not isinstance(expected, list) or not expected:
        return _fail('expected batch is empty or not a list')
    if len(expected) != len(set(expected)):
        return _fail('expected batch contains duplicate IDs')

    try:
        report = _load_json(index_path, reject_duplicate_keys=True)
    except (IOError, OSError, ValueError) as exc:
        return _fail('cannot read report: {0}'.format(exc))
    actual_map = report.get('BlackBull') if isinstance(report, dict) else None
    if not isinstance(actual_map, dict) or not actual_map:
        return _fail('report has no BlackBull cases')
    actual = list(actual_map)
    if len(actual) != len(expected) or set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        return _fail(
            'report ID mismatch: expected={0} actual={1} missing={2} extra={3}'.format(
                len(expected), len(actual), missing, extra,
            )
        )

    bad = sorted(
        case_id
        for case_id, result in actual_map.items()
        if result.get('behavior') not in ACCEPTED_BEHAVIOR
        or result.get('behaviorClose') not in ACCEPTED_CLOSE
    )
    if bad:
        return _fail('non-passing cases: {0}'.format(bad))
    print(json.dumps({'verified_count': len(actual), 'bad_count': len(bad)}))
    return 0


def main(arguments):
    if len(arguments) == 4 and arguments[1] == 'resolve':
        return resolve(arguments[2], arguments[3])
    if len(arguments) == 4 and arguments[1] == 'manifest':
        return build_manifest(arguments[2], arguments[3])
    if len(arguments) == 4 and arguments[1] == 'verify':
        return verify_report(arguments[2], arguments[3])
    return _fail(
        'usage: autobahn_cases.py '
        '{resolve SELECTOR OUTPUT|manifest RESOLVED OUTPUT|verify EXPECTED INDEX}'
    )


if __name__ == '__main__':
    sys.exit(main(sys.argv))
