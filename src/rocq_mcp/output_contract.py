"""Single Python owner for the public output and goal-selector enums."""

VIEW_STATUSES = frozenset({"complete", "partial_recoverable", "partial_unrecoverable"})
FIELD_VIEW_KINDS = frozenset({"inline", "live_state", "stored", "unavailable"})
GOAL_GROUPS = frozenset({"focused", "stack_left", "stack_right", "shelved", "given_up"})
GOAL_PARTS = frozenset({"names", "type", "definition", "conclusion"})
RANGE_STATUS_DATA = "data"
RANGE_STATUS_EOF = "eof"
RANGE_STATUSES = frozenset({RANGE_STATUS_DATA, RANGE_STATUS_EOF})
WARNING_FILTER_INCLUDE = "include_warnings"
WARNING_FILTER_EXCLUDE = "exclude_warnings"
WARNING_FILTER_NOT_APPLICABLE = "not_applicable"
WARNING_FILTER_MODES = frozenset({
    WARNING_FILTER_INCLUDE, WARNING_FILTER_EXCLUDE, WARNING_FILTER_NOT_APPLICABLE,
})
