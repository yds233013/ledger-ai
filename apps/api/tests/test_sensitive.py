"""The sensitive-identifier detector.

Two failure modes matter here and they pull in opposite directions. Missing a
real account number breaks the promise the upload consent makes. Rejecting an
ordinary bank export is worse in practice: a check that fires on normal files
is one people work around, and then it protects nobody.

So the false-positive cases below are not filler. A real statement carries a
masked "Account Number" column, six-digit reference numbers, nine-digit
confirmation codes and five-figure amounts, and every one of those must pass.
"""

from __future__ import annotations

import io

from ledgerai.services import sensitive
from ledgerai.services.sensitive import Category

# Test vectors only. Every number below is either a published test value or was
# constructed to satisfy a checksum; none belongs to anybody.
VISA_TEST_PAN = "4111111111111111"
MASTERCARD_TEST_PAN = "5555555555554444"
AMEX_TEST_PAN = "378282246310005"
VALID_ABA = "021000021"  # JPMorgan Chase's published routing number.
VALID_IBAN = "GB82WEST12345698765432"  # The IBAN registry's own example.


def _rows(csv_text: str):
    import csv

    reader = csv.reader(io.StringIO(csv_text))
    headers = next(reader)
    return headers, list(reader)


def _scan(csv_text: str) -> sensitive.Findings:
    headers, rows = _rows(csv_text)
    return sensitive.scan_rows(headers, rows)


class TestChecksums:
    def test_known_test_pans_pass_luhn(self):
        for pan in (VISA_TEST_PAN, MASTERCARD_TEST_PAN, AMEX_TEST_PAN):
            assert sensitive.luhn_valid(pan)

    def test_a_pan_with_one_digit_changed_fails_luhn(self):
        assert not sensitive.luhn_valid("4111111111111112")

    def test_a_published_routing_number_passes_aba(self):
        assert sensitive.aba_valid(VALID_ABA)

    def test_all_zeros_is_not_a_routing_number(self):
        # Arithmetically it passes; it is not a routing number, and a file full
        # of zero-padding must not be refused.
        assert not sensitive.aba_valid("000000000")

    def test_the_registry_example_iban_validates(self):
        assert sensitive.iban_valid(VALID_IBAN)

    def test_an_iban_with_a_transposed_pair_fails_mod_97(self):
        assert not sensitive.iban_valid("GB82WEST12345698765423")

    def test_never_issued_ssn_ranges_are_implausible(self):
        for digits in ("000123456", "666123456", "900123456", "123006789", "123450000"):
            assert not sensitive.ssn_plausible(digits)

    def test_an_ordinary_ssn_shape_is_plausible(self):
        assert sensitive.ssn_plausible("123456789")


class TestMasking:
    def test_the_forms_statements_actually_use_are_masked(self):
        for value in ("••••4821", "****4821", "XXXX-4821", "xxxx4821", "x4821", "4821", ""):
            assert sensitive.is_masked(value), value

    def test_a_full_pan_is_not_masked(self):
        assert not sensitive.is_masked(VISA_TEST_PAN)

    def test_a_masked_pan_is_never_reported(self):
        findings = _scan(
            "Date,Description,Amount,Account Number\n"
            "2026-01-04,COFFEE,-4.50,****1111\n"
        )
        assert not findings.rejected


class TestHeadersAreSignalsNotVerdicts:
    """A sensitive column name raises scrutiny. It never rejects on its own."""

    def test_a_bank_export_with_a_masked_account_column_is_accepted(self):
        findings = _scan(
            "Date,Description,Amount,Account Number,Reference\n"
            "2026-01-04,WHOLE FOODS MKT,-64.21,••••4821,201847362\n"
            "2026-01-05,TRANSIT AUTHORITY,-2.75,••••4821,201847363\n"
            "2026-01-06,PAYROLL DEPOSIT,2400.00,••••4821,201847364\n"
        )
        assert not findings.rejected

    def test_every_sensitive_header_with_masked_values_is_accepted(self):
        for header in ("SSN", "Card Number", "Routing Number", "IBAN", "Account No"):
            findings = _scan(f"Date,Amount,{header}\n2026-01-04,-4.50,****1234\n")
            assert not findings.rejected, header

    def test_an_empty_value_under_a_sensitive_header_is_accepted(self):
        findings = _scan("Date,Amount,SSN\n2026-01-04,-4.50,\n")
        assert not findings.rejected

    def test_a_sensitive_header_with_an_unmasked_pan_is_rejected(self):
        findings = _scan(f"Date,Amount,Card Number\n2026-01-04,-4.50,{VISA_TEST_PAN}\n")
        assert findings.rejected
        assert findings.categories == [Category.PAYMENT_CARD.value]

    def test_a_routing_number_counts_only_under_a_routing_column(self):
        under_header = _scan(f"Date,Amount,Routing Number\n2026-01-04,-4.50,{VALID_ABA}\n")
        assert Category.US_ROUTING.value in under_header.categories

        # The same nine digits in a reference column are a reference number.
        # About one nine-digit value in ten satisfies the ABA checksum by
        # chance, so rejecting on the digits alone would refuse ordinary files.
        as_reference = _scan(f"Date,Amount,Reference\n2026-01-04,-4.50,{VALID_ABA}\n")
        assert not as_reference.rejected

    def test_an_undashed_ssn_counts_only_under_an_ssn_column(self):
        under_header = _scan("Date,Amount,SSN\n2026-01-04,-4.50,123456789\n")
        assert Category.US_SSN.value in under_header.categories

        as_reference = _scan("Date,Amount,Confirmation\n2026-01-04,-4.50,123456789\n")
        assert not as_reference.rejected


class TestOrdinaryFilesAreNotRejected:
    def test_a_plain_statement_passes(self):
        findings = _scan(
            "Date,Description,Amount\n"
            "2026-01-04,WHOLE FOODS MKT #10234,-64.21\n"
            "2026-01-05,SQ *BLUE BOTTLE,-6.75\n"
            "2026-01-06,RENT PAYMENT,-2350.00\n"
        )
        assert not findings.rejected

    def test_long_transaction_ids_are_not_identifiers(self):
        findings = _scan(
            "Date,Amount,Transaction ID\n"
            "2026-01-04,-64.21,20260104998877665544\n"
            "2026-01-05,-6.75,20260105998877665545\n"
        )
        assert not findings.rejected

    def test_large_amounts_are_never_identifier_candidates(self):
        findings = _scan(
            "Date,Description,Amount,Balance\n"
            "2026-01-04,WIRE TRANSFER,-4111111111111111,4111111111111111\n"
        )
        assert not findings.rejected, "an amount column is not scanned for identifiers"

    def test_dates_in_several_formats_are_not_identifiers(self):
        findings = _scan(
            "Posted Date,Description,Amount\n"
            "2026-01-04,COFFEE,-4.50\n"
            "01/04/2026,COFFEE,-4.50\n"
            "20260104,COFFEE,-4.50\n"
        )
        assert not findings.rejected


class TestDetection:
    def test_an_unmasked_pan_anywhere_is_rejected(self):
        findings = _scan(f"Date,Description,Amount\n2026-01-04,CARD {VISA_TEST_PAN},-4.50\n")
        assert findings.categories == [Category.PAYMENT_CARD.value]

    def test_a_pan_written_with_spaces_or_dashes_is_still_found(self):
        for written in ("4111 1111 1111 1111", "4111-1111-1111-1111"):
            findings = _scan(f"Date,Description,Amount\n2026-01-04,{written},-4.50\n")
            assert findings.rejected, written

    def test_a_dashed_ssn_is_rejected_without_any_column_hint(self):
        findings = _scan("Date,Description,Amount\n2026-01-04,REF 123-45-6789,-4.50\n")
        assert findings.categories == [Category.US_SSN.value]

    def test_an_iban_is_rejected_without_any_column_hint(self):
        findings = _scan(f"Date,Description,Amount\n2026-01-04,TO {VALID_IBAN},-4.50\n")
        assert findings.categories == [Category.IBAN.value]

    def test_an_identifier_in_the_header_row_is_found(self):
        findings = _scan(f"Date,Amount,{VISA_TEST_PAN}\n2026-01-04,-4.50,x\n")
        assert findings.rejected

    def test_several_categories_are_all_reported(self):
        findings = _scan(
            f"Date,Description,Amount\n"
            f"2026-01-04,{VISA_TEST_PAN},-4.50\n"
            f"2026-01-05,123-45-6789,-4.50\n"
            f"2026-01-06,{VALID_IBAN},-4.50\n"
        )
        assert findings.categories == [
            Category.IBAN.value,
            Category.PAYMENT_CARD.value,
            Category.US_SSN.value,
        ]


class TestTheReportLeaksNothing:
    def test_a_report_carries_only_categories_counts_and_guidance(self):
        findings = _scan(
            f"Date,Description,Amount\n"
            f"2026-01-04,{VISA_TEST_PAN},-4.50\n"
            f"2026-01-05,{MASTERCARD_TEST_PAN},-9.99\n"
        )
        report = findings.as_report()
        assert set(report) == {"categories", "counts", "guidance"}
        assert report["counts"] == {Category.PAYMENT_CARD.value: 2}

        # The matched values, the row they were on and the column they were in
        # must not be recoverable from anything the caller receives.
        serialized = repr(report)
        for leak in (VISA_TEST_PAN, MASTERCARD_TEST_PAN, "4111", "5554444", "Description"):
            assert leak not in serialized, leak

    def test_the_findings_object_itself_holds_no_values(self):
        findings = _scan(f"Date,Description,Amount\n2026-01-04,{VISA_TEST_PAN},-4.50\n")
        assert set(vars(findings)) == {"counts"}
        assert VISA_TEST_PAN not in repr(findings)

    def test_guidance_says_what_to_do_without_naming_the_value(self):
        findings = _scan(f"Date,Description,Amount\n2026-01-04,{VISA_TEST_PAN},-4.50\n")
        guidance = findings.guidance()
        assert "mask" in guidance
        assert VISA_TEST_PAN not in guidance


class TestFreeText:
    """Receipt OCR has no columns, so only self-standing classes apply."""

    def test_a_pan_in_ocr_text_is_rejected(self):
        assert sensitive.scan_text(f"VISA {VISA_TEST_PAN} APPROVED").rejected

    def test_an_ordinary_receipt_passes(self):
        text = "BLUE BOTTLE COFFEE\n2026-01-04 09:14\nLatte 5.25\nTax 0.47\nTOTAL 5.72\nAUTH 004821"
        assert not sensitive.scan_text(text).rejected

    def test_a_masked_card_line_passes(self):
        assert not sensitive.scan_text("VISA ****1111 APPROVED").rejected

    def test_a_bare_nine_digit_run_in_ocr_text_is_left_alone(self):
        # No column context exists here, so nine digits are just nine digits.
        assert not sensitive.scan_text(f"TERMINAL {VALID_ABA}").rejected


class TestBounds:
    def test_scanning_stops_at_the_cell_limit(self):
        rows = [["x"] * 10 for _ in range(100)]
        findings = sensitive.scan_rows(["a"] * 10, rows, max_cells=50)
        assert not findings.rejected

    def test_scan_csv_parses_and_scans_bytes(self):
        data = f"Date,Description,Amount\n2026-01-04,{VISA_TEST_PAN},-4.50\n".encode()
        assert sensitive.scan_csv(data).rejected

    def test_scan_csv_accepts_a_semicolon_delimited_export(self):
        data = (
            "Date;Description;Amount;Account Number\n"
            "2026-01-04;WHOLE FOODS;-64.21;••••4821\n"
            "2026-01-05;TRANSIT;-2.75;••••4821\n"
        ).encode()
        assert not sensitive.scan_csv(data).rejected


class TestFreeTextProximity:
    """PDF statements carry no headers, so a nearby label supplies the context.

    Only the classes that need context change behaviour. Cards, dashed SSNs and
    IBANs are caught anywhere, exactly as they are in a CSV.
    """

    def test_a_labelled_routing_number_is_rejected(self):
        findings = sensitive.scan_free_text("Account Number 021000021 branch 12")
        assert Category.US_ROUTING.value in findings.categories

    def test_an_unlabelled_nine_digit_run_is_left_alone(self):
        # Roughly one nine-digit value in ten satisfies the ABA checksum by
        # chance, and a statement is full of reference numbers.
        assert not sensitive.scan_free_text("Reference 021000021 posted 12 Aug").rejected

    def test_a_labelled_ssn_is_rejected(self):
        findings = sensitive.scan_free_text("SSN 123456789 held on file")
        assert Category.US_SSN.value in findings.categories

    def test_a_masked_value_under_a_label_is_accepted(self):
        # Every real statement prints its own masked account line.
        assert not sensitive.scan_free_text("Account Number ****4821").rejected
        assert not sensitive.scan_free_text("Account Number \u2022\u2022\u2022\u20224821").rejected

    def test_a_card_number_is_caught_without_any_label(self):
        assert sensitive.scan_free_text(f"PAYMENT {VISA_TEST_PAN} APPROVED").rejected

    def test_an_iban_is_caught_without_any_label(self):
        assert sensitive.scan_free_text(f"transfer to {VALID_IBAN}").rejected

    def test_a_dashed_ssn_is_caught_without_any_label(self):
        assert sensitive.scan_free_text("ref 123-45-6789 on file").rejected

    def test_an_ordinary_statement_line_is_accepted(self):
        text = "12 Aug SANDBOX GROCERS 0042 -42.10 1,904.55 ref 201847362"
        assert not sensitive.scan_free_text(text).rejected

    def test_a_sort_code_label_raises_scrutiny(self):
        assert sensitive.scan_free_text("Sort code 021000021").rejected

    def test_the_report_still_carries_no_values(self):
        findings = sensitive.scan_free_text("Account Number 021000021")
        assert "021000021" not in repr(findings.as_report())


class TestCsvBehaviourIsUnchanged:
    """The proximity rule is additive. These assert the CSV path did not move."""

    def test_a_bare_nine_digit_reference_column_is_still_accepted(self):
        findings = _scan("Date,Amount,Reference\n2026-01-04,-4.50,021000021\n")
        assert not findings.rejected

    def test_a_routing_header_still_rejects(self):
        findings = _scan(f"Date,Amount,Routing Number\n2026-01-04,-4.50,{VALID_ABA}\n")
        assert Category.US_ROUTING.value in findings.categories

    def test_a_masked_bank_export_is_still_accepted(self):
        findings = _scan(
            "Date,Description,Amount,Account Number\n"
            "2026-01-04,WHOLE FOODS MKT,-64.21,\u2022\u2022\u2022\u20224821\n"
            "2026-01-05,TRANSIT,-2.75,\u2022\u2022\u2022\u20224821\n"
            "2026-01-06,PAYROLL,2400.00,\u2022\u2022\u2022\u20224821\n"
        )
        assert not findings.rejected

    def test_scan_text_still_ignores_a_bare_nine_digit_run(self):
        assert not sensitive.scan_text(f"TERMINAL {VALID_ABA}").rejected
