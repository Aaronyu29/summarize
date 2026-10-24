"""Bounded, public issuer evidence enrichment; no model calls or ledger writes.

``transport`` is a requests-compatible object exposing ``get``. Source bytes,
extracted text and first observation are immutable. Dates describe the source,
never the fetch clock; an undated document is not evidence of past availability.
"""
from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .common import decimal_value, parse_time, stable_id, utc_now
from .market import symbol_value


VERSION = "official-primary-v1"
MAX_BYTES = 8 * 1024 * 1024
TSM_PAGE = "https://investor.tsmc.com/english/quarterly-results/{year}/q{quarter}"
GOOGLE_PDF = "https://s206.q4cdn.com/479360582/files/doc_financials/{year}/q{quarter}/{year}q{quarter}-alphabet-earnings-release.pdf"
SOXX_PAGE = "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf"
SOXX_HOLDINGS = "https://www.ishares.com/us/products/239705/ishares-semiconductor-etf/latest-holdings.csv"
DATE = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s*\d{4}"
NUMBER = r"\(?-?\d[\d,]*(?:\.\d+)?\)?"


class EvidenceError(ValueError):
    pass


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.skip = [], [], 0
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.skip += 1
        if tag == "a" and attrs.get("href"):
            self.anchor = [attrs["href"], []]
        if tag in {"p", "div", "tr", "br", "li", "h1", "h2", "h3"} and not self.skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.skip = max(0, self.skip - 1)
        if tag == "a" and self.anchor:
            self.links.append((self.anchor[0], " ".join(self.anchor[1])))
            self.anchor = None
        if tag in {"p", "div", "tr", "li", "h1", "h2", "h3"} and not self.skip:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)
            if self.anchor:
                self.anchor[1].append(data.strip())

    @property
    def text(self):
        return "\n".join(re.sub(r"[^\S\n]+", " ", line).strip()
                         for line in "".join(self.parts).splitlines() if line.strip())


def _date(value):
    value = re.sub(r"\s+", " ", value.strip())
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise EvidenceError("SOURCE_DATE_UNPARSEABLE")


def _published(value):
    # Day-only disclosures are conservatively available at the end of that day.
    return _date(value) + "T23:59:59+00:00"


def _available_day(day, now):
    # NAV/holdings dates describe a completed fund valuation day, not an
    # intraday executable observation or an exact issuer publication timestamp.
    return parse_time(day + "T23:59:59+00:00") <= parse_time(now)


def _allowed(url):
    p = urlsplit(url)
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
        return False
    return ((p.hostname == "investor.tsmc.com" and p.path.startswith("/english/")) or
            (p.hostname == "s206.q4cdn.com" and p.path.startswith("/479360582/files/doc_financials/")) or
            (p.hostname == "www.ishares.com" and p.path.startswith("/us/products/239705/")) or
            (p.hostname == "www.blackrock.com" and p.path.startswith("/varnish-api/blk-one01-product-data/")))


def _text(raw, content_type):
    if raw.startswith(b"%PDF"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted or len(reader.pages) > 100:
            raise EvidenceError("SOURCE_PDF_UNSUPPORTED")
        return "\n".join(page.extract_text() or "" for page in reader.pages), "pdf"
    value = raw.decode("utf-8-sig")
    if "urn:schemas-microsoft-com:office:spreadsheet" in value[:500]:
        sheets = _spreadsheet(raw)
        return "\n\n".join(name + "\n" + "\n".join("\t".join(row) for row in rows)
                             for name, rows in sheets.items()), "spreadsheetml"
    if "html" in content_type or re.match(r"\s*(?:<!doctype html|<html)", value, re.I):
        parser = _HTML()
        parser.feed(value)
        return parser.text, "html"
    return value, "text"


def _fetch(store, url, now, transport):
    if not _allowed(url):
        raise EvidenceError("SOURCE_URL_NOT_ALLOWED")
    cached = store.get("primary_fetch", stable_id(url))
    if cached and cached.get("parser") == VERSION and parse_time(cached["observed_at"]) <= parse_time(now) < parse_time(cached["expires_at"]):
        source = store.get("primary_source", cached["source_id"])
        if source:
            return source
    original = url
    for _ in range(4):
        if not _allowed(url):
            raise EvidenceError("SOURCE_REDIRECT_NOT_ALLOWED")
        response = transport.get(url, timeout=(5, 25), allow_redirects=False,
            headers={"User-Agent": "RhinoInvestEvidence/1.0 (public issuer financial research)", "Accept": "application/pdf,text/html,text/csv,application/xml,*/*"})
        if response.status_code in {301, 302, 303, 307, 308}:
            url = urljoin(url, response.headers.get("Location", ""))
            continue
        if response.status_code != 200:
            raise EvidenceError("SOURCE_HTTP_" + str(response.status_code))
        raw = response.content
        if not raw or len(raw) > MAX_BYTES:
            raise EvidenceError("SOURCE_SIZE_INVALID")
        content, kind = _text(raw, response.headers.get("Content-Type", ""))
        # A bot page or login document cannot silently become report evidence.
        if len(content.strip()) < 60 or (urlsplit(url).path.lower().endswith(".pdf") and kind != "pdf"):
            raise EvidenceError("SOURCE_CONTENT_INVALID")
        raw_hash = hashlib.sha256(raw).hexdigest()
        key = stable_id(VERSION, original, raw_hash)
        source = store.get("primary_source", key)
        if not source:
            source = {"id": key, "source_url": original, "resolved_url": url, "raw_sha256": raw_hash,
                "raw_base64": base64.b64encode(raw).decode("ascii"), "content": content,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "format": kind,
                "observed_at": now, "parser": VERSION}
            store.put("primary_source", key, source, immutable=True)
        store.put("primary_fetch", stable_id(original), {"source_id": key, "observed_at": now,
            "expires_at": (parse_time(now) + timedelta(hours=6)).isoformat(), "parser": VERSION})
        return source
    raise EvidenceError("SOURCE_REDIRECT_LIMIT")


def _evidence(store, source, *, published_at=None, period_end=None):
    key = stable_id("official-primary-evidence", VERSION, source["source_url"], source["sha256"], published_at, period_end)
    previous = store.get("external_evidence", key)
    if previous:
        return {k: previous[k] for k in ("id", "source_url", "sha256", "raw_sha256", "published_at", "period_end", "observed_at")}
    if len(source["content"].encode("utf-8")) > 1024 * 1024:
        raise EvidenceError("SOURCE_EXTRACTED_TEXT_TOO_LARGE")
    record = {k: source[k] for k in ("source_url", "content", "sha256", "raw_sha256", "observed_at", "parser")}
    record.update(id=key, source_id=source["id"], published_at=published_at, period_end=period_end,
                  as_of=published_at or source["observed_at"], reviewed_by=VERSION)
    store.put("external_evidence", key, record, immutable=True)
    return {k: record[k] for k in ("id", "source_url", "sha256", "raw_sha256", "published_at", "period_end", "observed_at")}


def _gap(gaps, code, field, source_url=None):
    value = {"code": code, "field": field, "source": "official-primary", "blocking": True}
    if source_url:
        value["source_url"] = source_url
    if value not in gaps:
        gaps.append(value)


def _fact(metric, value, evidence, excerpt, *, year, quarter=None, currency="USD", price_unit=None, locator="issuer financial table"):
    end = f"{year}-12-31" if quarter is None else f"{year}-{quarter * 3:02d}-" + ("30" if quarter in {2, 3} else "31")
    start = f"{year}-01-01" if quarter is None else f"{year}-{(quarter - 1) * 3 + 1:02d}-01"
    return {"metric": metric, "value": str(decimal_value(value)), "currency": currency, "price_unit": price_unit,
        "period_type": "annual" if quarter is None else "quarterly", "fiscal_year": year,
        "fiscal_period": "FY" if quarter is None else f"Q{quarter}", "period_start": start,
        "period_end": end, "as_of": end, "published_at": evidence["published_at"],
        "value_kind": "reported", "evidence_refs": [evidence["id"]], "source_url": evidence["source_url"],
        "source_locator": locator, "source_excerpt": excerpt}


def _line(text, pattern):
    found = re.search(pattern, text, re.I | re.M)
    if not found:
        raise EvidenceError("SOURCE_TABLE_LAYOUT_CHANGED")
    return found


def _tsm(store, out, now, transport):
    moment = parse_time(now)
    # At most the latest completed quarter and one preceding quarter are queried;
    # a future/unreleased page is never silently assigned last year's numbers.
    q = (moment.month - 1) // 3
    year = moment.year if q else moment.year - 1
    q = q or 4
    targets = [(year, q), (year if q > 1 else year - 1, q - 1 if q > 1 else 4), (moment.year - 1, 4)]
    seen, quarterly_done = set(), False
    for year, q in targets:
        if (year, q) in seen or (quarterly_done and q != 4):
            continue
        seen.add((year, q))
        page_url = TSM_PAGE.format(year=year, quarter=q)
        try:
            page = _fetch(store, page_url, now, transport)
            parser = _HTML()
            parser.feed(base64.b64decode(page["raw_base64"]).decode("utf-8-sig"))
            links = {label.strip(): urljoin(page_url, url) for url, label in parser.links}
            management = _fetch(store, links["Management Report"], now, transport)
            financial = _fetch(store, links["Financial Statements"], now, transport)
            text = management["content"]
            if "TSMC" not in text or "US$ per ADR unit" not in text:
                raise EvidenceError("SOURCE_ISSUER_OR_UNIT_MISMATCH")
            _line(text, rf"\b{q}Q{str(year)[-2:]}\b")
            date_match = _line(text, DATE)
            published_at = _published(date_match.group())
            if parse_time(published_at) > moment:
                raise EvidenceError("SOURCE_NOT_AVAILABLE_AS_OF")
            end = f"{year}-{q*3:02d}-" + ("30" if q in {2, 3} else "31")
            ev = _evidence(store, management, published_at=published_at, period_end=end)
            out["primary_evidence"].append(ev)
            # Explicit management table headings identify ordinary and ADR rows.
            eps_ntd = _line(text, r"EPS\s*\(NT\$ per common share\)[^\n]*")
            eps_adr = _line(text, r"\(US\$ per ADR unit\)[^\n]*")
            ordinary = re.findall(NUMBER, eps_ntd.group().split(")", 1)[1])
            adr = re.findall(NUMBER, eps_adr.group().split(")", 1)[1])
            if len(ordinary) < 3 or len(adr) < 3:
                raise EvidenceError("SOURCE_TABLE_LAYOUT_CHANGED")
            fs_adr = _line(financial["content"], r"Earnings per ADR\s*-\s*Diluted[^\n]*").group()
            # Ignore the footnote marker (2); compare the explicit USD column,
            # never derive the ADR value by multiplying provider EPS by five.
            fs_values = re.findall(NUMBER, fs_adr.split("$", 1)[1])
            if not fs_values or decimal_value(fs_values[0]) != decimal_value(adr[0]):
                raise EvidenceError("SOURCE_EPS_RECONCILIATION_FAILED")
            for metric, value, currency, unit, excerpt in (
                ("eps_diluted", adr[0], "USD", "per_adr", eps_adr.group()),
                ("eps_diluted", ordinary[0], "TWD", "per_share", eps_ntd.group())):
                out["primary_financials"]["facts"].append(_fact(metric, value, ev, excerpt, year=year, quarter=q, currency=currency, price_unit=unit))
            if q == 4 and len(adr) == 5 and len(ordinary) == 7:
                annual_adr = re.findall(r"Earnings per ADR\s*-\s*Diluted[^\n]*", financial["content"], re.I)
                if len(annual_adr) != 2 or decimal_value(re.findall(NUMBER, annual_adr[1].split("$", 1)[1])[0]) != decimal_value(adr[3]):
                    raise EvidenceError("SOURCE_EPS_RECONCILIATION_FAILED")
                for value, currency, unit, excerpt in ((adr[3], "USD", "per_adr", eps_adr.group()),
                                                       (ordinary[5], "TWD", "per_share", eps_ntd.group())):
                    out["primary_financials"]["facts"].append(_fact("eps_diluted", value, ev, excerpt, year=year, currency=currency, price_unit=unit))
            revenue = _line(text, r"Net Revenue\s*\(US\$ billions\)[^\n]*")
            values = re.findall(NUMBER, revenue.group().split(")", 1)[1])
            out["primary_financials"]["facts"].append(_fact("revenue", decimal_value(values[0]) * 10**9, ev,
                revenue.group(), year=year, quarter=q, locator="management report; USD billions converted to USD"))
            if q == 4 and len(values) == 7:
                out["primary_financials"]["facts"].append(_fact("revenue", decimal_value(values[5]) * 10**9, ev,
                    revenue.group(), year=year, locator="management report; USD billions converted to USD"))
            # This file may be a revised audited FS. Without an exact publication
            # date, its historical availability is unknown; preserve observation.
            ratio = _line(financial["content"], r"1\s+ADR equals\s+(\d+(?:\.\d+)?)\s+ordinary shares\.")
            fev = _evidence(store, financial, period_end=end)
            out["primary_evidence"].append(fev)
            instrument = {"is_adr": True, "quote_price_unit": "per_adr", "quote_currency": "USD",
                "stock_eps_valuation_applicable": True, "ordinary_shares_per_adr": ratio.group(1),
                "underlying_currency": "TWD", "evidence_refs": [fev["id"], ev["id"]],
                "source_locator": "FS ADR footnote and management EPS row",
                "source_excerpt": ratio.group(), "unit_source_excerpt": eps_adr.group()}
            if not out["primary_financials"]["instrument"]:
                out["primary_financials"]["instrument"] = instrument
            quarterly_done = True
        except Exception as exc:
            _gap(out["primary_gaps"], str(exc) if isinstance(exc, EvidenceError) else "PRIMARY_SOURCE_UNAVAILABLE",
                 "TSM_" + str(year) + "Q" + str(q), page_url)
    if quarterly_done:
        for gap in out["primary_gaps"]:
            if gap.get("field", "").startswith("TSM_") and gap["code"] in {"SOURCE_HTTP_404", "SOURCE_HTTP_403"}:
                gap.update(blocking=False, code="NEWER_RELEASE_NOT_AVAILABLE")
    instrument = out["primary_financials"]["instrument"]
    if instrument:
        from .research import bind_instrument_identity
        current = store.get("instrument_identity", "TSM") or {}
        metadata = {"is_adr": True, "ordinary_shares_per_adr": instrument["ordinary_shares_per_adr"],
                    "underlying_currency": "TWD", "currency": "USD", "price_unit": "per_adr"}
        binding = current if current.get("metadata") == metadata and current.get("evidence_refs") == instrument["evidence_refs"] else bind_instrument_identity(
            store, "TSM", metadata, evidence_refs=instrument["evidence_refs"], reviewed_by=VERSION, reviewed_at=now)
        out.update(reviewed_identity=binding, adr_conversion_verified=True)


def _google(store, out, now, transport):
    moment = parse_time(now)
    q = (moment.month - 1) // 3
    year = moment.year if q else moment.year - 1
    q = q or 4
    # Alphabet reports after quarter-end; fall back to preceding quarter when
    # the next release does not exist, rather than guessing a publication date.
    targets = [(year, q), (year if q > 1 else year - 1, q - 1 if q > 1 else 4), (moment.year - 1, 4)]
    seen, quarterly_done = set(), False
    for year, q in targets:
        if (year, q) in seen or (quarterly_done and q != 4):
            continue
        seen.add((year, q))
        url = GOOGLE_PDF.format(year=year, quarter=q)
        try:
            source = _fetch(store, url, now, transport)
            text = source["content"]
            if not re.search(r"Alphabet Inc\.\s*\(NASDAQ:\s*GOOG,\s*GOOGL\)", text):
                raise EvidenceError("SOURCE_ISSUER_OR_UNIT_MISMATCH")
            date_match = _line(text, DATE)
            published_at = _published(date_match.group())
            if parse_time(published_at) > moment:
                raise EvidenceError("SOURCE_NOT_AVAILABLE_AS_OF")
            end = f"{year}-{q*3:02d}-" + ("30" if q in {2, 3} else "31")
            if not re.search(r"quarter ended\s+" + re.escape(datetime.strptime(end, "%Y-%m-%d").strftime("%B")) + r"\s+" + str(int(end[-2:])) + r",\s*" + str(year), text, re.I):
                raise EvidenceError("SOURCE_PERIOD_MISMATCH")
            ev = _evidence(store, source, published_at=published_at, period_end=end)
            out["primary_evidence"].append(ev)
            excerpt = _line(text, r"Diluted net income per (?:common )?share[^\n]*").group()
            values = re.findall(NUMBER, excerpt)
            expected = 4 if q == 4 else 2
            if len(values) != expected:
                raise EvidenceError("SOURCE_TABLE_LAYOUT_CHANGED")
            out["primary_financials"]["facts"].append(_fact("eps_diluted", values[1], ev, excerpt,
                year=year, quarter=q, price_unit="per_share"))
            if q == 4:
                out["primary_financials"]["facts"].append(_fact("eps_diluted", values[3], ev, excerpt,
                    year=year, price_unit="per_share"))
            # Header explicitly says USD millions except per-share information.
            unit = _line(text, r"in millions, except for per share information[^\n]*").group()
            out["primary_financials"]["instrument"] = {"is_adr": False, "quote_currency": "USD",
                "quote_price_unit": "per_share", "stock_eps_valuation_applicable": True,
                "evidence_refs": [ev["id"]], "source_locator": "GOOG/GOOGL issuer heading and diluted common EPS table",
                "source_excerpt": excerpt, "unit_source_excerpt": unit}
            for metric, pattern in (("revenue", r"^Revenues[^\n]*"),
                                    ("operating_income", r"^Operating income[^\n]*"),
                                    ("other_income_net", r"^Other income \(expense\), net[^\n]*")):
                match = _line(text, pattern)
                vals = re.findall(NUMBER, match.group())
                if len(vals) != expected:
                    raise EvidenceError("SOURCE_TABLE_LAYOUT_CHANGED")
                for index, quarter in ((1, q), (3, None)) if q == 4 else ((1, q),):
                    value = vals[index].replace("(", "-").replace(")", "")
                    out["primary_financials"]["facts"].append(_fact(metric, decimal_value(value.replace(",", "")) * 10**6,
                        ev, match.group(), year=year, quarter=quarter, locator="USD millions converted to USD; issuer highlights"))
            nonop = re.search(r"Other income reflected[^\n]*(?:\n[^\n]*){0,2}", text)
            if nonop:
                out["primary_financials"].setdefault("non_operating_items", []).append({"source_excerpt": nonop.group(),
                    "period_end": end, "evidence_refs": [ev["id"]], "warning": "Reported GAAP EPS includes investment gains; not normalized or recurring earnings."})
            quarterly_done = True
        except Exception as exc:
            # A candidate release that has not been published is not a missing
            # required fact when the preceding official quarter is available.
            code = str(exc) if isinstance(exc, EvidenceError) else "PRIMARY_SOURCE_UNAVAILABLE"
            _gap(out["primary_gaps"], code, out["symbol"] + "_" + str(year) + "Q" + str(q), url)
    if quarterly_done:
        for gap in out["primary_gaps"]:
            if gap.get("field", "").startswith(out["symbol"] + "_") and gap["code"] in {"SOURCE_HTTP_404", "SOURCE_HTTP_403"}:
                gap.update(blocking=False, code="NEWER_RELEASE_NOT_AVAILABLE")
    # The shared release is a consolidated issuer statement, not the separate
    # A/B/C allocation table. Class EPS can differ; native quotes and identities
    # remain those of the requested ticker and are never copied across classes.
    for fact in out["primary_financials"]["facts"]:
        if fact["metric"] == "eps_diluted":
            fact.update(eps_scope="consolidated_all_classes", source_rationale=
                "Issuer consolidated diluted EPS is a historical group anchor; it does not certify exact Class A, B or C EPS. Class allocations may differ and require the separate filing table.")
    out["primary_financials"]["eps_scope"] = "consolidated_all_classes"
    out["primary_financials"]["class_eps_reconciliation"] = {"status": "not_verified",
        "requested_symbol": out["symbol"], "share_class": "Class C" if out["symbol"] == "GOOG" else "Class A",
        "reason": "Shared group earnings release does not separately resolve class-specific diluted EPS."}


def _spreadsheet(raw):
    text = raw.decode("utf-8-sig")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise EvidenceError("SOURCE_XML_UNSUPPORTED")
    # The issuer's SpreadsheetML hyperlinks contain bare ampersands. Escape
    # those only; the exact original bytes remain in primary_source.
    text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", text)
    root = ET.fromstring(text)
    ns = {"s": "urn:schemas-microsoft-com:office:spreadsheet"}
    return {sheet.attrib.get("{" + ns["s"] + "}Name"): [
        ["".join(cell.itertext()).strip() for cell in row.findall("s:Cell", ns)]
        for row in sheet.findall("s:Table/s:Row", ns)] for sheet in root.findall("s:Worksheet", ns)}


def _soxx(store, out, now, transport):
    page = _fetch(store, SOXX_PAGE, now, transport)
    text = page["content"]
    if "iShares Semiconductor ETF" not in text:
        raise EvidenceError("SOURCE_ISSUER_OR_UNIT_MISMATCH")
    page_ev = _evidence(store, page)
    out["primary_evidence"].append(page_ev)
    valuation = {"issuer_verified": False, "nav": None, "pe": None, "holdings": None, "corporate_actions": [],
                 "price_unit": "per_share", "currency": "USD"}
    out["etf_valuation"] = valuation
    for field, pattern in (("pe", r"P/E\s*Ratio\s*(?P<value>\d[\d,.]*)\s*as of\s*(?P<date>" + DATE + ")"),
                           ("expense_ratio", r"Expense Ratio:\s*Fees as stated in the prospectus\s*(?P<value>\d+(?:\.\d+)?)%")):
        m = re.search(pattern, text, re.I)
        if m:
            value = {"value": str(decimal_value(m.group("value").replace(",", ""))), "evidence_refs": [page_ev["id"]],
                     "source_excerpt": m.group(), "source_url": SOXX_PAGE}
            if field == "pe":
                value.update(as_of=_date(m.group("date")), methodology="issuer-published fund P/E; not operating-company annual EPS")
                if not _available_day(value["as_of"], now):
                    _gap(out["primary_gaps"], "SOURCE_NOT_AVAILABLE_AS_OF", "SOXX_PE", SOXX_PAGE)
                    continue
            else:
                value["unit"] = "percent_per_year"
            valuation[field] = value
    action = re.search(r"On\s+(?P<announced>" + DATE + r"),\s*iShares filed to issue a forward stock split.*?Shares of SOXX will begin trading on a split-adjusted basis on\s+(?P<effective>" + DATE + r")\.", text, re.S)
    if action:
        effective, announced = _date(action.group("effective")), _date(action.group("announced"))
        if _available_day(announced, now):
            valuation["corporate_actions"].append({"type": "split", "announced_at": announced, "effective_at": effective,
                "status": "announced_not_effective" if effective > parse_time(now).date().isoformat() else "effective_requires_unit_check",
                "new_shares_per_old": None, "evidence_refs": [page_ev["id"]], "source_excerpt": action.group(),
                "automatic_price_adjustment": False})
    try:
        holdings = _fetch(store, SOXX_HOLDINGS, now, transport)
        csv_rows = list(csv.reader(io.StringIO(holdings["content"])))
        asof = _date(next(row[1] for row in csv_rows if row and row[0] == "Fund Holdings as of"))
        if not _available_day(asof, now):
            raise EvidenceError("SOURCE_NOT_AVAILABLE_AS_OF")
        header_index = next(i for i, row in enumerate(csv_rows) if row and row[0] == "Ticker")
        header = csv_rows[header_index]
        rows = []
        for values in csv_rows[header_index + 1:]:
            row = dict(zip(header, values))
            if row.get("Asset Class") != "Equity":
                continue
            weight = decimal_value(row["Weight (%)"].replace(",", ""))
            if weight < 0 or weight > 100:
                raise EvidenceError("SOURCE_HOLDINGS_WEIGHT_INVALID")
            rows.append({"symbol": row["Ticker"], "weight": str(weight / 100), "weight_pct": str(weight), "name": row.get("Name")})
        if not rows or len({r["symbol"] for r in rows}) != len(rows):
            raise EvidenceError("SOURCE_HOLDINGS_INVALID")
        hev = _evidence(store, holdings, period_end=asof)
        out["primary_evidence"].append(hev)
        total = sum(decimal_value(r["weight"]) for r in rows)
        if not Decimal("0.95") <= total <= Decimal("1.01"):
            raise EvidenceError("SOURCE_HOLDINGS_COVERAGE_INCOMPLETE")
        valuation["holdings"] = {"as_of": asof, "rows": rows, "coverage": str(total),
            "concentration": {"top10_weight": str(sum(sorted((decimal_value(r["weight"]) for r in rows), reverse=True)[:10]))},
            "evidence_refs": [hev["id"]], "source_excerpt": csv_rows[1][0] + ',"' + csv_rows[1][1] + '"',
            "source_url": SOXX_HOLDINGS}
    except Exception as exc:
        _gap(out["primary_gaps"], str(exc) if isinstance(exc, EvidenceError) else "PRIMARY_SOURCE_UNAVAILABLE", "SOXX_HOLDINGS", SOXX_HOLDINGS)
    try:
        parser = _HTML()
        parser.feed(base64.b64decode(page["raw_base64"]).decode("utf-8-sig"))
        download_url = next(urljoin(SOXX_PAGE, url) for url, _ in parser.links if "component=fundDownload" in unescape(url))
        download = _fetch(store, unescape(download_url), now, transport)
        sheets = _spreadsheet(base64.b64decode(download["raw_base64"]))
        pe_date = (valuation.get("pe") or {}).get("as_of")
        dated = [(_date(row[0]), row) for row in sheets.get("Historical", [])[1:] if len(row) >= 2]
        eligible = [(date, row) for date, row in dated if _available_day(date, now)]
        selected = next(((date, row) for date, row in eligible if date == pe_date), None)
        if selected is None and eligible:
            selected = max(eligible, key=lambda item: item[0])
        if not selected:
            raise EvidenceError("SOURCE_NAV_DATE_UNAVAILABLE")
        date, row = selected
        dev = _evidence(store, download, period_end=date)
        out["primary_evidence"].append(dev)
        valuation["nav"] = {"value": str(decimal_value(row[1])), "currency": "USD", "price_unit": "per_share", "as_of": date,
            "evidence_refs": [dev["id"]], "source_locator": "SpreadsheetML Historical sheet: As Of / NAV per Share",
            "source_url": download["source_url"], "source_excerpt": row[1], "adjustment": "issuer NAV per share; no local split transformation"}
    except Exception as exc:
        _gap(out["primary_gaps"], str(exc) if isinstance(exc, EvidenceError) else "PRIMARY_SOURCE_UNAVAILABLE", "SOXX_NAV", SOXX_PAGE)
    for name in ("nav", "pe", "holdings"):
        if not valuation.get(name):
            _gap(out["primary_gaps"], "ETF_" + name.upper() + "_UNAVAILABLE", "SOXX_" + name.upper())
    if valuation["nav"] and valuation["pe"] and valuation["holdings"]:
        dates = {valuation[name]["as_of"] for name in ("nav", "pe", "holdings")}
        if len(dates) != 1:
            _gap(out["primary_gaps"], "ETF_SOURCE_DATE_MISMATCH", "SOXX_VALUATION")
        elif decimal_value(valuation["nav"]["value"]) > 0 and decimal_value(valuation["pe"]["value"]) > 0:
            valuation["issuer_verified"] = True
    valuation["evidence_refs"] = sorted({ref for name in ("nav", "pe", "holdings")
                                          for ref in (valuation.get(name) or {}).get("evidence_refs", [])})
    out["primary_financials"]["instrument"] = {"is_adr": False, "quote_currency": "USD", "quote_price_unit": "per_share",
        "stock_eps_valuation_applicable": False, "asset_type": "etf", "evidence_refs": [page_ev["id"]],
        "source_excerpt": "iShares Semiconductor ETF", "source_locator": "issuer fund heading"}
    if valuation.get("nav"):
        out["primary_financials"]["instrument"].update(
            source_excerpt="NAV per Share", unit_source_excerpt="iShares Semiconductor ETF",
            evidence_refs=[page_ev["id"], *valuation["nav"]["evidence_refs"]],
            source_locator="issuer fund heading and official Historical NAV per Share column")


def enrich_primary_fundamentals(store, symbol, fundamentals, *, now=None, transport=None):
    """Return public facts separately from native provider data and forecasts.

    Only TSM, GOOG, GOOGL and SOXX are implemented. ``now`` is the information cutoff;
    fetching a newer source never changes its source date to that cutoff.
    """
    symbol = symbol_value(symbol)
    now = parse_time(now or utc_now()).isoformat()
    out = copy.deepcopy(fundamentals or {})
    out["symbol"] = symbol
    if symbol not in {"TSM", "GOOG", "GOOGL", "SOXX"}:
        return out
    out.update(primary_financials={"symbol": symbol, "facts": [], "instrument": {}}, primary_evidence=[], primary_gaps=[])
    out.pop("etf_valuation", None)
    if transport is None:
        import requests
        transport = requests.Session()
    try:
        {"TSM": _tsm, "GOOG": _google, "GOOGL": _google, "SOXX": _soxx}[symbol](store, out, now, transport)
    except Exception as exc:
        _gap(out["primary_gaps"], str(exc) if isinstance(exc, EvidenceError) else "PRIMARY_SOURCE_UNAVAILABLE", symbol)
    out["primary_evidence"] = list({item["id"]: item for item in out["primary_evidence"]}.values())
    # No observation timestamp in this semantic digest: identical facts cannot
    # queue a fresh model review on every fetch or service restart.
    out["primary_evidence_hash"] = stable_id(VERSION, symbol,
        [(item["id"], item["sha256"]) for item in out["primary_evidence"]],
        out["primary_financials"], out.get("etf_valuation"))
    retry = any(gap.get("blocking") is True for gap in out["primary_gaps"])
    out["primary_refresh_after"] = (parse_time(now) + timedelta(hours=6 if retry else 24)).isoformat()
    return out
