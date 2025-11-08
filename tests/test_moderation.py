from moder_telegram.moderation import is_bad_message


def test_positive():
    assert is_bad_message("This contains badword inside")


def test_negative():
    assert not is_bad_message("Hello, this is safe text")


def test_word_boundaries():
    # should detect whole word but not substring inside other word
    assert is_bad_message("That is a badword.")
    assert not is_bad_message("This is badwording and should be allowed by boundary check")


def test_regex_entry():
    # allow regex entries prefixed with re:
    banned = ["re:spam\\d+"]
    assert is_bad_message("spam123", banned=banned)
    assert not is_bad_message("spam abc", banned=banned)
