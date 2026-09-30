"""
MQTT 5.0 Topic Name and Topic Filter matching conformance tests.

Verifies topic matching rules against the MQTT 5.0 OASIS Standard.

Reference: MQTT Version 5.0, OASIS Standard
  §4.7  Topic Names and Topic Filters
  §4.7.1  Topic wildcards
  §4.7.1.1  Topic level separator
  §4.7.1.2  Single-level wildcard '+'
  §4.7.1.3  Multi-level wildcard '#'
  §4.7.2  Topic semantic and usage
  §4.8  Shared Subscriptions

Key rules:
  - §4.7.1.1: The topic level separator is '/' (U+002F SOLIDUS).
  - §4.7.1.2: '+' matches exactly one complete topic level.
    Example: 'sensors/+/temperature' matches 'sensors/room1/temperature'
    but NOT 'sensors/room1/upstairs/temperature' (two levels)
    and NOT 'sensors/temperature' (no level for '+').
  - §4.7.1.3: '#' matches any number of complete topic levels including zero.
    '#' MUST be the last character in the Topic Filter (or the only character).
    Example: 'sensors/#' matches 'sensors', 'sensors/room1',
    'sensors/room1/temperature', etc.
  - §4.7.1.3: '#' is valid as the entire Topic Filter (matches all topics).
    This is a "super-wildcard" subscription.
  - §4.7.2: Topic Names MUST NOT contain wildcard characters ('+' or '#').
  - §4.8: Shared Subscriptions use $share/{ShareName}/{TopicFilter} format.
"""

import pytest

from blackbull.mqtt.messages import (
    topic_matches_filter,
    validate_topic_name,
    validate_topic_filter,
)


# ============================================================================
# §4.7.1.1 — Topic Level Separator
# ============================================================================

class TestTopicLevelSeparator:
    """§4.7.1.1 — The topic level separator is '/' (U+002F)."""

    def test_topic_levels_delimited_by_slash(self):
        """§4.7.1.1 — Levels are separated by '/'."""
        parts = 'sensors/room1/temperature'.split('/')
        assert parts == ['sensors', 'room1', 'temperature']


# ============================================================================
# §4.7.1.2 — Single-Level Wildcard '+'
# ============================================================================

class TestWildcardMatching:
    """§4.7.1.2/§4.7.1.3 — '+' and '#' wildcard matching rules."""

    @pytest.mark.parametrize("filter_str,topic,should_match", [
        # §4.7.1.2 — '+' matches exactly one level
        ('sensors/+/temperature', 'sensors/room1/temperature', True),
        ('sensors/+/temperature', 'sensors/room2/temperature', True),
        ('sensors/+/temperature', 'sensors/basement/temperature', True),
        # '+' does NOT match two levels
        ('sensors/+/temperature', 'sensors/room1/upstairs/temperature', False),
        # '+' must match a level; cannot be zero levels
        ('sensors/+/temperature', 'sensors/temperature', False),
        # Multiple '+' wildcards in one filter
        ('+/+/temperature', 'building/room/temperature', True),
        ('+/+/temperature', 'building/temperature', False),   # not enough levels
        # '+' mixed with explicit levels
        ('+/status', 'sensor1/status', True),
        ('+/status', 'sensor1/reading', False),
        # '+' at start or middle of topic level
        ('sensors/room+/temperature', 'sensors/room1/temperature', False),
        # '+' is a complete level, not partial
        ('sensors/room1/temperature', 'sensors/room1/temperature', True),
        # §4.7.1.3 — '#' matches any number of subsequent levels
        ('sensors/#', 'sensors/temperature', True),
        ('sensors/#', 'sensors/room1/temperature', True),
        ('sensors/#', 'sensors/room1/upstairs/temperature', True),
        ('sensors/#', 'sensors', True),  # matches zero additional levels
        # '#' as the only character matches ALL topics
        ('#', 'any/topic/at/all', True),
        ('#', 'single', True),
        ('#', '', False),  # empty string is not a valid topic name
        # '#' must be at the end
        ('sensors/room1/#', 'sensors/room1/temperature', True),
        ('sensors/room1/#', 'sensors/room1/a/b/c/d', True),
        # Non-matching cases
        ('sensors/room1/#', 'other/topic', False),
        ('specific/topic', 'specific/other', False),
    ])
    def test_single_level_wildcard_matching(self, filter_str, topic, should_match):
        """§4.7.1.2/§4.7.1.3 — '+' and '#' wildcard matching rules."""
        assert topic_matches_filter(topic, filter_str) == should_match, \
            f"Filter '{filter_str}' vs topic '{topic}': expected {should_match}"


# ============================================================================
# §4.7.1.3 — Multi-Level Wildcard '#'
# ============================================================================

class TestWholeFilterWildcards:
    """A wildcard as the entire Topic Filter (§4.7.1.2/§4.7.1.3), and the
    '#' placement grammar (§4.7.1.3: last character, preceded by '/' or
    alone)."""

    @pytest.mark.parametrize('checks', [
        pytest.param([('foo', '#', True), ('foo/bar', '#', True), ('a/b/c/d/e', '#', True)],
                     id='hash-entire-filter'),
        pytest.param([('temperature', '+', True), ('humidity', '+', True),
                      ('sensors/temperature', '+', False)],
                     id='plus-entire-filter'),
    ])
    def test_hash_as_entire_filter_matches_everything(self, checks):
        """§4.7.1.3/§4.7.1.2 — '#' as the entire Topic Filter is a
        super-wildcard; '+' as the entire filter matches any single-level
        topic."""
        for topic, filter_str, expected in checks:
            assert topic_matches_filter(topic, filter_str) is expected

    @pytest.mark.parametrize('filter_str,match', [
        pytest.param('sensors/#/invalid', '#.*last', id='hash-not-last'),
        pytest.param('sensors#', '#.*slash|#.*only|#.*preced', id='hash-needs-slash-or-alone'),
        pytest.param('sensors/#/more/#', '#', id='multiple-hash'),
    ])
    def test_hash_must_be_last_character(self, filter_str, match):
        """§4.7.1.3 — '#' syntax rules: must be the last character, must be
        preceded by '/' or be alone, and only one '#' per filter."""
        with pytest.raises(ValueError, match=match):
            validate_topic_filter(filter_str)


# ============================================================================
# §4.7.1.2, §4.7.1.3 — Combined wildcards
# ============================================================================

class TestCombinedWildcards:
    """Both '+' and '#' in the same Topic Filter."""

    @pytest.mark.parametrize("filter_str,topic,should_match", [
        # '+' followed by '#'
        ('sensors/+/temperature/#', 'sensors/room1/temperature/reading', True),
        ('sensors/+/temperature/#', 'sensors/room1/temperature', True),
        ('sensors/+/temperature/#', 'sensors/room1/humidity', False),
        # '#' after '+'
        ('+/#', 'anything/at/all', True),
        ('+/#', 'single', True),
        # Multiple '+' with '#'
        ('+/+/+/#', 'a/b/c/d/e', True),
        ('+/+/+/#', 'a/b', False),
    ])
    def test_combined_wildcard_matching(self, filter_str, topic, should_match):
        """Combined '+' and '#' wildcard matching."""
        assert topic_matches_filter(topic, filter_str) == should_match, \
            f"Filter '{filter_str}' vs topic '{topic}': expected {should_match}"


# ============================================================================
# §4.7.2 — Topic Name validation (PUBLISH topics)
# ============================================================================

class TestTopicNameValidation:
    """§4.7.2 — Topic Names MUST NOT contain wildcard characters.

    Wildcards ('+' and '#') are only allowed in Topic Filters (SUBSCRIBE).
    Topic Names in PUBLISH packets must be literal.
    """

    @pytest.mark.parametrize('name,expected', [
        pytest.param('sensors/+/temperature', False, id='plus-invalid'),
        pytest.param('sensors/#', False, id='hash-invalid'),
        pytest.param('building/floor3/room42/temperature', True, id='literal-valid'),
        pytest.param('', False, id='empty-name-invalid'),
        pytest.param('sensors/room1/', True, id='trailing-slash'),
        pytest.param('/sensors/room1', True, id='leading-slash'),
        pytest.param('sensors/\x00room', False, id='null-character'),
        pytest.param('temperature', True, id='single-level-topic'),
        pytest.param('sensors/room1/temperature', True, id='multiple-levels'),
    ])
    def test_empty_topic_name_is_invalid(self, name, expected):
        """validate_topic_name validity table (§4.7.2 names exclude wildcard
        characters, §4.7.1.1 level separator, §1.5.4 null excluded)."""
        assert validate_topic_name(name) is expected


# ============================================================================
# §4.8 — Shared Subscriptions
# ============================================================================

class TestSharedSubscriptions:
    """§4.8 — Shared Subscriptions: $share/{ShareName}/{TopicFilter}.

    Shared subscriptions distribute messages across a group of subscribers.
    Only one subscriber in the group receives each message.
    """

    @pytest.mark.parametrize('topic,filter_str', [
        pytest.param('sensors/temperature', '$share/group1/sensors/temperature',
                     id='shared-format'),
        pytest.param('sensors/room1/temperature', '$share/group1/sensors/+/temperature',
                     id='shared-with-wildcard'),
        pytest.param('$SYS/broker/uptime', '$SYS/broker/uptime',
                     id='dollar-topic-literal'),
    ])
    def test_shared_subscription_format(self, topic, filter_str):
        """§4.8 — shared-subscription matching $share/{ShareName}/{TopicFilter}
        and §4.7.2 — '$'-prefixed topics match literal subscriptions."""
        assert topic_matches_filter(topic, filter_str) is True

    def test_shared_subscription_share_name_no_wildcards(self):
        """§4.8 — $share and ShareName MUST NOT contain wildcards."""
        with pytest.raises(ValueError, match='[Ss]hare.*wildcard|[Ss]hare.*\\+'):
            validate_topic_filter('$share/group+/sensors/temperature')
        with pytest.raises(ValueError, match='[Ss]hare.*wildcard|[Ss]hare.*#'):
            validate_topic_filter('$share/group#/sensors/temperature')


# ============================================================================
# §4.7.1 — Edge cases and boundary conditions
# ============================================================================

class TestTopicMatchingEdgeCases:
    """Edge cases for topic matching."""

    @pytest.mark.parametrize('checks', [
        pytest.param([('foo/bar', 'foo/bar', True), ('foo/bar', 'foo/baz', False)],
                     id='exact-match'),
        pytest.param([('a/b/c', 'a/#', True), ('a', 'a/#', True)],
                     id='trailing-slash-then-hash'),
    ])
    def test_exact_match_no_wildcards(self, checks):
        """Exact-match filters compare equal; 'a/#' matches 'a', 'a/b',
        'a/b/c' etc. (§4.7.1.3)."""
        for topic, filter_str, expected in checks:
            assert topic_matches_filter(topic, filter_str) is expected

    def test_dollar_prefixed_topics(self):
        """§4.7.2 — Topics starting with '$' are typically reserved
        for server-internal use.  They don't match '#' or '+' by default
        (server policy determines this)."""
        # MQTT 5.0 §4.7.2: A Topic Filter starting with '$' is a special case.
        # A subscription to '#' will NOT receive messages published to
        # topics starting with '$'.  A subscription to '+/monitor' will
        # NOT receive messages published to '$SYS/monitor'.
        assert topic_matches_filter('$SYS/broker/uptime', '#') is False, \
            "Topics starting with '$' must NOT match '#' by default"
        assert topic_matches_filter('$SYS/monitor', '+/monitor') is False, \
            "Topics starting with '$' must NOT match '+' by default"

    def test_hash_only_valid_for_topic_filter_not_name(self):
        """§4.7.2 — '#' is valid in a Topic Filter but NOT in a Topic Name."""
        assert validate_topic_filter('#') is True
        assert validate_topic_name('#') is False
