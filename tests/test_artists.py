import pytest
from music_manager.artists import canonical_artist, clean_title, filename_hint, validate_artists

RULES = [{'name': 'Drake', 'aliases': ['드레이크']}, {'name': 'Rihanna', 'aliases': ['리한나']}]


@pytest.mark.parametrize('filename,expected', [
    ('Wu-Tang Forever - Drake (128k)', ('Drake', 'Wu-Tang Forever')),
    ('Drake - Wu-Tang Forever [320kbps]', ('Drake', 'Wu-Tang Forever')),
    ('Wu-Tang Forever — 드레이크 (128k)', ('Drake', 'Wu-Tang Forever')),
    ('Drake - Song (Live) (320k)', ('Drake', 'Song (Live)')),
    ('Song (Remix feat. Rihanna) - Drake', ('Drake', 'Song (Remix feat. Rihanna)')),
    ('Rihanna - Song - Remix', ('Rihanna', 'Song - Remix')),
    ('Drakeford - Song', ('Drakeford', 'Song')),
    ('Drake - Rihanna', ('', 'Drake - Rihanna')),
    ('Drake - Drake', ('', 'Drake - Drake')),
    ('A song about Drake', ('', 'A song about Drake')),
])
def test_dictionary_filename_recognition(filename, expected):
    assert filename_hint(filename, RULES) == expected


def test_dictionary_validation_and_unicode_names():
    rules = validate_artists([{'name': ' Drake ', 'aliases': ['DRAKE', '드레이크', ' 드레이크 ']}])
    assert rules == [{'name': 'Drake', 'aliases': ['드레이크']}]
    assert canonical_artist('드레이크', rules) == 'Drake'
    assert canonical_artist('ＤＲＡＫＥ', rules) == 'Drake'
    with pytest.raises(ValueError, match='겹칩니다'):
        validate_artists([{'name': 'Drake'}, {'name': 'Other', 'aliases': ['ＤＲＡＫＥ']}])
    with pytest.raises(ValueError, match='별칭'):
        validate_artists([{'name': 'Drake', 'aliases': ['a'] * 21}])
    with pytest.raises(ValueError):
        validate_artists([{'name': 'Drake', 'aliases': [12]}])
    with pytest.raises(ValueError):
        validate_artists([{'name': 'Bad\nName'}])
    with pytest.raises(ValueError):
        validate_artists({})


def test_preserve_version_and_clean_only_quality():
    assert clean_title('Song (Live) [128kbps] (Remix)') == 'Song (Live) (Remix)'
    assert clean_title('Song (Part 2)') == 'Song (Part 2)'


@pytest.mark.parametrize('text,expected', [
    ('Wu-Tang Forever 128k', 'Wu-Tang Forever'),
    ('Wu-Tang Forever - Drake 256k', 'Wu-Tang Forever - Drake'),
    ('Song (128k) [256kbps]', 'Song'),
    ('Song (256 kb/s)', 'Song'),
    ('Song [256kbps / 44.1kHz]', 'Song'),
    ('Song - 256K', 'Song'),
    ('Song_256k', 'Song'),
    ('Song （２５６ｋ）', 'Song'),
    ('Song (Live) 128k (Remix feat. Artist)', 'Song (Live) (Remix feat. Artist)'),
    ('Song 128 256 (Part 2)', 'Song 128 256 (Part 2)'),
    ('128king - K256', '128king - K256'),
    ('Blink-182 - 18 and Life', 'Blink-182 - 18 and Life'),
    ('24K Magic', '24K Magic'),
    ('100K (Remix)', '100K (Remix)'),
    ('Song 128k / 256k', 'Song'),
])
def test_quality_markers_with_or_without_brackets(text, expected):
    assert clean_title(text) == expected
    assert clean_title(clean_title(text)) == expected
