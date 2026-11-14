import pytest

import delivery


@pytest.fixture(autouse=True)
def no_default_number(monkeypatch):
    monkeypatch.setattr(delivery, "PHONE", "")
    monkeypatch.setattr(delivery, "COUNTRY_CODE", "+61")


def test_whatsapp_numbers_get_the_country_code():
    assert delivery.wa_number("0400 000 000") == "+61400000000"
    assert delivery.wa_number("+61 400 000 000") == "+61400000000"
    assert delivery.wa_number("+44 7700 900000") == "+447700900000"


def test_lists_are_cleaned_and_saved():
    r = delivery.recipients_save(["+61 400 000 000", "+61400000000", "someone@example.com"], [],
                                 sms=["+61 400 000 000"])
    assert r["phones"] == ["+61400000000", "someone@example.com"]
    assert r["sms"] == ["+61400000000"]


def test_bad_entries_are_refused():
    with pytest.raises(RuntimeError, match="not a phone number"):
        delivery.recipients_save(["12"], [])
    with pytest.raises(RuntimeError, match="not an email"):
        delivery.recipients_save(["+61400000000"], ["nope"])


def test_paused_entries_are_skipped_but_one_must_stay_on():
    r = delivery.recipients_save(["+61400000000", "+61400000001"], [], paused={"phones": ["+61 400 000 001"]})
    assert delivery.recipients(active=True)["phones"] == ["+61400000000"]
    assert r["paused"]["phones"] == ["+61400000001"]
    with pytest.raises(RuntimeError, match="at least one"):
        delivery.recipients_save(["+61400000000"], [], paused={"phones": ["+61400000000"]})


def test_nowhere_to_send_yet():
    assert delivery.recipients()["phones"] == []
    assert "nowhere" in delivery.sends_to()
