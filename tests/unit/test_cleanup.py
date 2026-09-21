from blackbull._cleanup import combine_cleanup_errors


def test_single_exception_group_keeps_its_identity():
    error = ExceptionGroup("cleanup failed", [ValueError("close failed")])

    assert combine_cleanup_errors(error) is error


def test_multiple_base_exceptions_select_the_narrowest_group_type():
    ordinary = RuntimeError("close failed")
    cancelled = KeyboardInterrupt()

    exception_group = combine_cleanup_errors(ordinary, ValueError("join failed"))
    base_group = combine_cleanup_errors(ordinary, cancelled)

    assert type(exception_group) is ExceptionGroup
    assert type(base_group) is BaseExceptionGroup
