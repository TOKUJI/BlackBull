"""
MQTT 5.0 Topic Name and Topic Filter matching conformance tests — Sprint 52.

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

    def test_topic_with_single_level(self):
        """§4.7.1.1 — A single-level topic has no separator."""
        assert validate_topic_name('temperature') is True

    def test_topic_with_multiple_levels(self):
        """§4.7.1.1 — Multiple levels separated by '/'."""
        assert validate_topic_name('sensors/room1/temperature') is True

    def test_topic_levels_delimited_by_slash(self):
        """§4.7.1.1 — Levels are separated by '/'."""
        parts = 'sensors/room1/temperature'.split('/')
        assert parts == ['sensors', 'room1', 'temperature']


# ============================================================================
# §4.7.1.2 — Single-Level Wildcard '+'
# ============================================================================

class TestSingleLevelWildcard:
    """§4.7.1.2 — '+' matches exactly one complete topic level."""

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
    ])
    def test_single_level_wildcard_matching(self, filter_str, topic, should_match):
        """§4.7.1.2 — '+' wildcard matching rules."""
        assert topic_matches_filter(topic, filter_str) == should_match, \
            f"Filter '{filter_str}' vs topic '{topic}': expected {should_match}"

    def test_plus_as_entire_filter(self):
        """§4.7.1.2 — '+' as the entire Topic Filter matches any single-level topic."""
        assert topic_matches_filter('temperature', '+') is True
        assert topic_matches_filter('humidity', '+') is True
        assert topic_matches_filter('sensors/temperature', '+') is False


# ============================================================================
# §4.7.1.3 — Multi-Level Wildcard '#'
# ============================================================================

class TestMultiLevelWildcard:
    """§4.7.1.3 — '#' matches any number of complete topic levels including zero.

    '#' MUST be the last character in the Topic Filter.
    It MUST be preceded by '/' or be the only character.
    """

    @pytest.mark.parametrize("filter_str,topic,should_match", [
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
    def test_multi_level_wildcard_matching(self, filter_str, topic, should_match):
        """§4.7.1.3 — '#' wildcard matching rules."""
        assert topic_matches_filter(topic, filter_str) == should_match, \
            f"Filter '{filter_str}' vs topic '{topic}': expected {should_match}"

    def test_hash_as_entire_filter_matches_everything(self):
        """§4.7.1.3 — '#' as the entire Topic Filter is a super-wildcard."""
        assert topic_matches_filter('foo', '#') is True
        assert topic_matches_filter('foo/bar', '#') is True
        assert topic_matches_filter('a/b/c/d/e', '#') is True

    def test_hash_must_be_last_character(self):
        """§4.7.1.3 — '#' MUST be the last character in the Topic Filter."""
        with pytest.raises(ValueError, match='#.*last'):
            validate_topic_filter('sensors/#/invalid')

    def test_hash_must_be_preceded_by_slash_or_be_only(self):
        """§4.7.1.3 — '#' MUST be preceded by '/' or be the only character."""
        with pytest.raises(ValueError, match='#.*slash|#.*only|#.*preced'):
            validate_topic_filter('sensors#')  # '#' not preceded by '/'


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

    def test_topic_name_with_plus_is_invalid(self):
        """§4.7.2 — '+' is not valid in a Topic Name."""
        assert validate_topic_name('sensors/+/temperature') is False

    def test_topic_name_with_hash_is_invalid(self):
        """§4.7.2 — '#' is not valid in a Topic Name."""
        assert validate_topic_name('sensors/#') is False

    def test_literal_topic_name_is_valid(self):
        """§4.7.2 — A literal Topic Name without wildcards is valid."""
        assert validate_topic_name('building/floor3/room42/temperature') is True

    def test_empty_topic_name_is_invalid(self):
        """§4.7.2 — An empty string is not a valid Topic Name.

        (The MQTT spec says Topic Names are at least 1 character.)
        """
        assert validate_topic_name('') is False

    def test_topic_name_with_trailing_slash(self):
        """§4.7.1 — A trailing '/' is valid but denotes a zero-length final
        level (some brokers may reject this).  BlackBull accepts it as
        spec-compliant: '/' is a topic level separator, and zero-length
        levels are explicitly allowed in MQTT 5.0 §4.7.1.1 Note."""
        assert validate_topic_name('sensors/room1/') is True

    def test_topic_name_with_leading_slash(self):
        """§4.7.1 — A leading '/' creates a zero-length first level.
        MQTT 5.0 allows this (though it's atypical)."""
        assert validate_topic_name('/sensors/room1') is True

    def test_topic_name_with_null_character(self):
        """§1.5.4 — The null character (U+0000) MUST NOT be used in
        Topic Names or Topic Filters."""
        assert validate_topic_name('sensors/\x00room') is False


# ============================================================================
# §4.8 — Shared Subscriptions
# ============================================================================

class TestSharedSubscriptions:
    """§4.8 — Shared Subscriptions: $share/{ShareName}/{TopicFilter}.

    Shared subscriptions distribute messages across a group of subscribers.
    Only one subscriber in the group receives each message.
    """

    def test_shared_subscription_format(self):
        """§4.8 — Valid shared subscription format."""
        assert topic_matches_filter(
            'sensors/temperature',
            '$share/group1/sensors/temperature',
        ) is True

    def test_shared_subscription_with_wildcard(self):
        """§4.8 — Shared subscription with topic filter wildcards."""
        assert topic_matches_filter(
            'sensors/room1/temperature',
            '$share/group1/sensors/+/temperature',
        ) is True

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

    def test_exact_match_no_wildcards(self):
        """Exact string match for literal filters."""
        assert topic_matches_filter('foo/bar', 'foo/bar') is True
        assert topic_matches_filter('foo/bar', 'foo/baz') is False

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

    def test_dollar_prefixed_topics_match_literal(self):
        """§4.7.2 — A literal subscription to '$SYS/...' matches."""
        assert topic_matches_filter('$SYS/broker/uptime', '$SYS/broker/uptime') is True

    def test_topic_filter_with_trailing_slash_then_hash(self):
        """§4.7.1.3 — 'a/#' is valid and matches 'a', 'a/b', 'a/b/c' etc."""
        assert topic_matches_filter('a/b/c', 'a/#') is True
        assert topic_matches_filter('a', 'a/#') is True

    def test_multiple_hash_in_filter_invalid(self):
        """§4.7.1.3 — Only one '#' is allowed, and it MUST be the last character."""
        with pytest.raises(ValueError, match='#'):
            validate_topic_filter('sensors/#/more/#')

    def test_hash_only_valid_for_topic_filter_not_name(self):
        """§4.7.2 — '#' is valid in a Topic Filter but NOT in a Topic Name."""
        assert validate_topic_filter('#') is True
        assert validate_topic_name('#') is False
