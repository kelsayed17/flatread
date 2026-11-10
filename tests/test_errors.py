import pytest

from flatread import errors


@pytest.mark.parametrize(
    "error",
    [
        errors.EmptyBodyError,
        errors.UnknownLayoutError,
        errors.RaggedRowError,
        errors.TruncatedFileError,
        errors.ControlTotalError,
        errors.CheckError,
        errors.ErratumGoneError,
        errors.NotPublishedError,
        errors.CorruptArchiveError,
        errors.EncodingError,
        errors.CellError,
    ],
)
def test_every_error_is_a_value_error(error: type[Exception]) -> None:
    assert issubclass(error, errors.ReadError)
    assert issubclass(error, ValueError)


def test_cell_error_names_where_label_and_text() -> None:
    error = errors.CellError("line 4", "Rate", "x", "a number")
    assert str(error) == "line 4: the 'Rate' cell 'x' is not a number"
    assert (error.where, error.label, error.text) == ("line 4", "Rate", "x")
    assert isinstance(error, errors.UnknownLayoutError)
