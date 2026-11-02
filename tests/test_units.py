import pytest

from memmap.units import format_address, format_size, parse_address, parse_size


@pytest.mark.parametrize(
    "literal, expected",
    [
        (0x08000000, 0x08000000),
        ("0x0800_0000", 0x08000000),
        ("0x0800 0000", 0x08000000),
        ("134217728", 0x08000000),
        ("\t0x10\n", 16),
        (0, 0),
        ("0", 0),
    ],
)
def test_parse_address(literal, expected):
    assert parse_address(literal) == expected


@pytest.mark.parametrize(
    "literal", ["0xZZ", "banana", -1, True, 1.5, None, "+16", "_", " ", "", "0x", "0x_", "0x "]
)
def test_parse_address_rejects(literal):
    with pytest.raises(ValueError):
        parse_address(literal)


@pytest.mark.parametrize(
    "literal, expected",
    [
        (512, 512),
        ("512", 512),
        ("16K", 16384),
        ("1M", 1048576),
        ("512M", 536870912),
        ("4G", 4294967296),
        ("16k", 16384),
        ("0x1000", 4096),
        ("0x10K", 16384),
        ("16 K", 16384),
        (" 16KB ", 16384),
        ("1_024", 1024),
        ("0010", 10),
    ],
)
def test_parse_size(literal, expected):
    assert parse_size(literal) == expected


@pytest.mark.parametrize(
    "literal",
    ["7Q", "K", 0, -4, True, "", "1 6K", "0", "0K", "00", "0x0", "_", "__K", "0x_", "+16"],
)
def test_parse_size_rejects(literal):
    with pytest.raises(ValueError):
        parse_size(literal)


def test_format_address_grouping():
    assert format_address(0x40024_3FF, 8) == "0x4002 43FF"
    assert format_address(0x08000000, 8, separator="_") == "0x0800_0000"
    assert format_address(0x20000000, 16) == "0x0000 0000 2000 0000"
    assert format_address(0x12345, 5) == "0x1 2345"


@pytest.mark.parametrize(
    "size, expected",
    [
        (65536, "64 KB"),
        (1536 * 1024, "1.5 MB"),
        (1100 * 1024, "1100 KB"),
        (512, "512 B"),
        (1024, "1 KB"),
        (4 * 1024**3, "4 GB"),
        (1024**4, "1 TB"),
        (1536 * 1024**3, "1.5 TB"),
    ],
)
def test_format_size(size, expected):
    assert format_size(size) == expected
