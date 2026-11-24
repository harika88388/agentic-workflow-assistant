from app.core.validation import is_valid_email, normalize_time


def test_normalize_time_formats():
    cases = {"3 PM": "15:00", "3pm": "15:00", "3:30 p.m.": "15:30", "12 AM": "00:00", "12pm": "12:00", "9:05am": "09:05",
             "15:00": "15:00", "7:00": "07:00", "noon": "12:00", "midnight": "00:00", "00:00": "00:00"}
    for raw, want in cases.items():
        assert normalize_time(raw) == want, (raw, normalize_time(raw))


def test_normalize_time_rejects_garbage():
    for raw in ["25:00", "13pm", "0am", "12:61", "soon", "", None, "15", "tomorrow"]:
        assert normalize_time(raw) is None, raw


def test_email_validation():
    assert is_valid_email("professor@example.com") and is_valid_email("a.b+c@sub.example.co.in")
    for bad in ["Rahul", "a@b", "a@@b.com", "a..b@x.com", ".a@x.com", "a b@x.com", "", None, "x@y.c"]:
        assert not is_valid_email(bad), bad
