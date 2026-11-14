"""``flatread.sdmx``: SDMX's messages, each kind a piece set by class attributes, and its refusals."""

import datetime as dt
import io
import math
import re
import typing
import zipfile
from typing import Annotated, ClassVar

import msgspec
import pytest

from flatread import sdmx
from flatread.errors import LayoutError, ReadError
from flatread.fields import Label
from flatread.sdmx import SdmxCsv, SdmxData, SdmxObservations, SdmxSeries, SdmxStructure

# --- SDMX-CSV -------------------------------------------------------------------------------------------------------


class _Flow(msgspec.Struct, frozen=True, gc=False):
    key: str
    freq: str
    time_period: str
    obs_value: float | None
    obs_status: str | None
    action: str | None


_CSV: typing.Final = SdmxCsv(_Flow)
_HEADER = b"KEY,FREQ,TIME_PERIOD,OBS_VALUE,OBS_STATUS"


def test_each_column_is_its_field_upper_cased_and_action_may_be_left_out() -> None:
    body = _HEADER + b"\r\nEXR.D,D,2026-01-02,1.5,A\r\nEXR.D,D,2026-01-05,,M\r\n"
    assert _CSV.rows(body) == [
        _Flow("EXR.D", "D", "2026-01-02", 1.5, "A", None),
        _Flow("EXR.D", "D", "2026-01-05", None, "M", None),
    ]
    with_action = _HEADER + b",ACTION\r\nEXR.D,D,2026-01-02,1.5,A,I\r\n"
    assert _CSV.rows(with_action)[0].action == "I"


def test_sdmx_s_non_finite_names_are_values_and_a_header_alone_is_no_rows() -> None:
    body = _HEADER + b"\r\nK,D,1,NaN,A\r\nK,D,2,INF,A\r\nK,D,3,-INF,A\r\nK,D,4,-1.5e-3,A\r\n"
    values = [row.obs_value for row in _CSV.rows(body)]
    assert values[0] is not None
    assert math.isnan(values[0])
    assert values[1:] == [math.inf, -math.inf, -1.5e-3]
    assert _CSV.rows(_HEADER + b"\r\n") == []


@pytest.mark.parametrize("value", [b"inf", b"nan", b"1.5 ", b"1e400", b"1,5"])
def test_an_obs_value_sdmx_does_not_write_raises(value: bytes) -> None:
    quoted = b'"' + value + b'"' if b"," in value else value
    with pytest.raises(ReadError, match=r"^the OBS_VALUE cell .* is not a number: row 1$"):
        _CSV.frame(_HEADER + b"\r\nK,D,1," + quoted + b",A\r\n")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_HEADER + b"\r\nK,D,1,1.5,A", r"cut"),
        (b"KEY,FREQ,TIME_PERIOD,OBS_VALUE\r\nK,D,1,1.5\r\n", r"is none of the 2 the _Flow file has had"),
    ],
)
def test_a_cut_body_or_another_header_raises(body: bytes, message: str) -> None:
    with pytest.raises(ReadError, match=message):
        _CSV.frame(body)


def test_a_struct_without_obs_value_is_no_sdmx_csv() -> None:
    with pytest.raises(LayoutError, match=r"^_Dataflow has no obs_value field, which every SDMX-CSV holds$"):
        SdmxCsv(_Dataflow)


# --- Structure messages ---------------------------------------------------------------------------------------------


class _Dataflow(msgspec.Struct, frozen=True, gc=False):
    id: Annotated[str, Label("@id")]
    name: Annotated[str, Label("com:Name")]


class _Code(msgspec.Struct, frozen=True, gc=False):
    code_list: Annotated[str, Label("../@id")]
    code: Annotated[str, Label("@value")]
    description: Annotated[str, Label("structure:Description")]


class _Structure10[RowT: msgspec.Struct](SdmxStructure[RowT]):
    _version: ClassVar = "1.0"


_V21 = (
    b'<mes:Structure xmlns:mes="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"'
    b' xmlns:str="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure"'
    b' xmlns:com="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"><mes:Structures><str:Dataflows>'
    b'<str:Dataflow id="EXR"><com:Name>Exchange Rates</com:Name></str:Dataflow>'
    b"</str:Dataflows></mes:Structures></mes:Structure>"
)
_V10 = (
    b'<message:Structure xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"'
    b' xmlns:structure="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/structure"><message:CodeLists>'
    b'<structure:CodeList id="CL_FREQ"><structure:Code value="129">'
    b"<structure:Description>Monthly</structure:Description></structure:Code></structure:CodeList>"
    b"</message:CodeLists></message:Structure>"
)


def test_a_structure_message_reads_one_kind_of_structure_in_its_version_s_namespaces() -> None:
    assert SdmxStructure(_Dataflow, "dataflows").rows(_V21) == [_Dataflow("EXR", "Exchange Rates")]
    assert _Structure10(_Code, "codes").rows(_V10) == [_Code("CL_FREQ", "129", "Monthly")]


def test_a_message_of_the_other_version_raises() -> None:
    with pytest.raises(ReadError, match=r"the document's root is <\{http://www.SDMX.org/.*\}Structure>, not"):
        SdmxStructure(_Dataflow, "dataflows").rows(_V10)


def test_a_kind_of_structure_the_version_does_not_list_raises() -> None:
    with pytest.raises(LayoutError, match=r"^an SDMX-ML 2.1 structure message lists no codes, only \['dataflows'\]$"):
        SdmxStructure(_Code, "codes")


# --- Data messages --------------------------------------------------------------------------------------------------


class _Data[RowT: msgspec.Struct](SdmxData[RowT]):
    """A compact data message in a format's own ``x:`` names, read from the bare body."""

    _prefixes: ClassVar = {"x": "urn:x"}
    _root = "message:MessageGroup"
    _data_set = "x:DataSet"
    _observation = "x:Obs"
    _annotations = "x:Annotations"


class _Series(msgspec.Struct, frozen=True, gc=False):
    series_name: str
    freq: str
    title: str


class _Obs(msgspec.Struct, frozen=True, gc=False):
    series_name: str
    time_period: dt.date
    obs_value: float | None
    obs_status: str


class _SeriesOf(_Data[_Series], SdmxSeries[_Series]):
    _described: ClassVar = {"title": "Title"}


class _ObsOf[RowT: msgspec.Struct](_Data[RowT], SdmxObservations[RowT]):
    _statuses = ("A", "M")
    _absent = ("",)
    _date_format = "%Y-%m"


class _Zipped[RowT: msgspec.Struct](_ObsOf[RowT]):
    _member = re.compile(r"[^/]*\.xml")


_M = b'xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"'
_X = b'xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common" xmlns:x="urn:x"'
_NOTE = (
    b"<x:Annotations><common:Annotation><common:AnnotationType>Title</common:AnnotationType>"
    b"<common:AnnotationText>%s</common:AnnotationText></common:Annotation></x:Annotations>"
)


def _message(*series: bytes) -> bytes:
    return (
        b"<message:MessageGroup "
        + _M
        + b" "
        + _X
        + b'><x:DataSet id="D" xmlns:k="urn:k">'
        + b"".join(series)
        + b"</x:DataSet></message:MessageGroup>"
    )


def _one(name: bytes, obs: bytes, *, note: bytes = _NOTE % b"T") -> bytes:
    return b'<k:Series SERIES_NAME="' + name + b'" FREQ="M">' + note + obs + b"</k:Series>"


_TWO = (
    b'<x:Obs TIME_PERIOD="2026-01" OBS_VALUE="1.5" OBS_STATUS="A"/>'
    b'<x:Obs OBS_STATUS="M" OBS_VALUE="" TIME_PERIOD="2026-02"/>'
)


def test_a_data_message_reads_into_its_series_and_its_observations_long() -> None:
    body = _message(_one(b"S1", _TWO), _one(b"S2", b""))
    assert _SeriesOf(_Series, "D").rows(body) == [_Series("S1", "M", "T"), _Series("S2", "M", "T")]
    assert _ObsOf(_Obs, "D").rows(body) == [
        _Obs("S1", dt.date(2026, 1, 1), 1.5, "A"),
        _Obs("S1", dt.date(2026, 2, 1), None, "M"),
    ]


def test_observations_reach_polars_a_chunk_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdmx, "_CHUNK", 1)
    body = _message(_one(b"S1", _TWO), _one(b"S2", _TWO))
    assert [(row.series_name, row.obs_status) for row in _ObsOf(_Obs, "D").rows(body)] == [
        ("S1", "A"),
        ("S1", "M"),
        ("S2", "A"),
        ("S2", "M"),
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_message(b'<k:Series FREQ="M">' + _NOTE % b"T" + _TWO + b"</k:Series>"), r"carries no SERIES_NAME$"),
        (_message(_one(b"S1", _TWO + _NOTE % b"T", note=b"")), r"holds other than its annotations and observations"),
        (
            _message(_one(b"S1", _TWO.replace(b"/>", b' OBS_CONF="F"/>', 1))),
            r"other than \['TIME_PERIOD', 'OBS_VALUE', 'OBS_STATUS'\]",
        ),
    ],
)
def test_a_series_the_observations_cannot_read_raises(body: bytes, message: str) -> None:
    with pytest.raises(ReadError, match=message):
        _ObsOf(_Obs, "D").frame(body)


@pytest.mark.parametrize(
    "note",
    [
        b"",
        _NOTE.replace(b"Title", b"Other") % b"T",
        _NOTE % b"T<x:b/>",
        _NOTE.replace(b"</common:Annotation>", b"</common:Annotation>stray") % b"T",
        (_NOTE % b"T").replace(b"</x:Annotations>", b"<common:Annotation/></x:Annotations>"),
        (_NOTE % b"T")
        .replace(b"<common:Annotation>", b"<x:Annotation>")
        .replace(b"</common:Annotation>", b"</x:Annotation>"),
    ],
)
def test_a_series_without_exactly_its_annotations_raises(note: bytes) -> None:
    with pytest.raises(ReadError, match=r"^the series 'S1' of the D data set of the body lacks its annotations"):
        _SeriesOf(_Series, "D").frame(_message(_one(b"S1", b"", note=note)))


def _zip(*members: tuple[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in members:
            archive.writestr(zipfile.ZipInfo(name), data, compress_type=zipfile.ZIP_DEFLATED)
    return out.getvalue()


def test_a_zipped_message_is_read_from_its_one_member(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _zip(("T.xml", _message(_one(b"S1", _TWO))))
    assert len(_Zipped(_Obs, "D").rows(body)) == 2
    with pytest.raises(ReadError, match=r"^the zip holds 2 members matching \[\^/\]\*\\.xml, not one"):
        _Zipped(_Obs, "D").frame(_zip(("T.xml", b""), ("U.xml", b"")))
    monkeypatch.setattr(sdmx, "_INFLATED_MAX", 10)
    with pytest.raises(ReadError, match=r"^the zip's 'T.xml' inflates to \d+ bytes, over 10$"):
        _Zipped(_Obs, "D").frame(body)


def test_a_format_whose_names_or_struct_do_not_fit_raises() -> None:
    class _Unbound(_ObsOf[_Obs]):
        _observation = "y:Obs"

    with pytest.raises(LayoutError, match=r"^the element 'y:Obs' writes a prefix the format's namespaces do not bind$"):
        _Unbound(_Obs, "D")
    with pytest.raises(LayoutError, match=r"^_Series's fields after the key are \['FREQ', 'TITLE'\], not an obs"):
        _ObsOf(_Series, "D")


_GOOD = _one(b"S1", _TWO)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_message(_GOOD).replace(b"MessageGroup", b"Other"), r"^the root of the body is <\{.*\}Other>, not <"),
        (_message(_GOOD)[:-20], r"^the body is not well-formed XML"),
        (_message(_GOOD).replace(b'id="D"', b'id="E"'), r"^the body holds no D data set; it holds \['E'\]$"),
        (_message(), r"^the D data set of the body holds no series$"),
        (
            _message().replace(b"<x:DataSet", _GOOD + b"<x:DataSet").replace(b'xmlns:k="urn:k"', b""),
            r"of the body sits outside any <\{urn:x\}DataSet>$",
        ),
        (_message(b"<x:note/>", _GOOD), r"^in the D data set, the body holds b'<x:note.*' where a series should be$"),
        (_message(_GOOD, b"text", _GOOD), r"^in the D data set, the body holds b'<k:Series.*' where a series"),
        (_message(b"text", _GOOD), r"^in the D data set, the body holds 'text' where a series should be$"),
        (_message(_GOOD, b"<x:note/>"), r"^in the D data set, the body holds other than series$"),
        (_message(_GOOD, _GOOD), r"^the D data set of the body lists the series 'S1' twice$"),
        (
            _message(_GOOD).replace(b"</x:DataSet>", b'</x:DataSet><x:note/><x:DataSet id="E"/>'),
            r"^the body holds a data set somewhere other than after the last, in its root$",
        ),
        (
            _message(_GOOD).replace(b"</x:DataSet>", b"</x:DataSet>text"),
            r"^the body holds other than data sets after its last$",
        ),
        (
            _message(_GOOD).replace(b"</x:DataSet>", b"</x:DataSet><x:note/>"),
            r"^the body holds other than data sets after its last$",
        ),
        (
            _message()
            .replace(b'<x:DataSet id="D" xmlns:k="urn:k">', b'<x:a><x:DataSet id="D" xmlns:k="urn:k">')
            .replace(b"</x:DataSet>", b"</x:DataSet></x:a>"),
            r"^the body holds a data set somewhere other than after the last, in its root$",
        ),
        (_message(_GOOD.replace(b' FREQ="M"', b"")), r"carries \['SERIES_NAME'\], not \['FREQ', 'SERIES_NAME'\]$"),
    ],
)
def test_a_message_that_is_not_its_format_s_raises(body: bytes, message: str) -> None:
    with pytest.raises(ReadError, match=message):
        _SeriesOf(_Series, "D").frame(body)


@pytest.mark.parametrize(
    ("obs", "message"),
    [
        (
            b'OBS_STATUS="X" OBS_VALUE="1" TIME_PERIOD="2026-01"',
            r"\(status 'X', value '1'\) has no CL_OBS_STATUS status",
        ),
        (
            b'OBS_STATUS="A" OBS_VALUE="1" TIME_PERIOD="2026-1"',
            r"on '2026-1' in the body .* is not a day written %Y-%m$",
        ),
        (b'OBS_STATUS="A" OBS_VALUE="1e5" TIME_PERIOD="2026-01"', r"is not a number$"),
        (b'OBS_STATUS="M" OBS_VALUE="0" TIME_PERIOD="2026-01"', r"is not the format's no-value marker$"),
    ],
)
def test_an_observation_off_its_format_raises(obs: bytes, message: str) -> None:
    with pytest.raises(ReadError, match=rf"^the D observation of 'S1' .*{message}"):
        _ObsOf(_Obs, "D").frame(_message(_one(b"S1", b"<x:Obs " + obs + b"/>")))


def test_a_member_that_will_not_inflate_raises() -> None:
    body = bytearray(_zip(("T.xml", _message(*[_one(f"S{n}".encode(), _TWO) for n in range(50)]))))
    body[60] ^= 0xFF
    with pytest.raises(ReadError, match=r"^the zip's 'T.xml' will not inflate"):
        _Zipped(_Obs, "D").frame(bytes(body))
