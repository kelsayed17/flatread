"""``flatread.core``'s typing steps: a field's scalar type, and a body's closing line ending."""

from typing import Literal, NewType

import pytest

from flatread.core import check_ending, scalar_type
from flatread.errors import ReadError

_Code = NewType("_Code", str)


def test_scalar_type_unwraps_none_newtype_and_literal() -> None:
    assert scalar_type(float | None) is float
    assert scalar_type(_Code | None) is str
    assert scalar_type(Literal["a", "b"]) is str
    assert scalar_type(int) is int


def test_check_ending_refuses_a_cut_body() -> None:
    check_ending(b"a\n", final_line_ending=True)
    with pytest.raises(ReadError, match="it was cut"):
        check_ending(b"a\n", final_line_ending=False)
