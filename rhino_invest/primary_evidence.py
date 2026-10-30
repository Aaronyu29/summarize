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
import json
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
GOOGLE_10Q_2026Q2 = 'https://d18rn0p25nwr6d.cloudfront.net/CIK-0001652044/513183d2-787c-48f5-8da7-8a897b1649e0.html'
GOOGLE_SUBMISSIONS='https://data.sec.gov/submissions/CIK0001652044.json'
SOXX_PAGE = "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf"
SOXX_HOLDINGS = "https://www.ishares.com/us/products/239705/ishares-semiconductor-etf/latest-holdings.csv"
CIRCLE_IR = "https://investor.circle.com/overview/default.aspx"
SEC_ISSUERS = {"AMZN": "0001018724", "INTC": "0000050863", "MSFT": "0000789019"}
QQQ_PAGE = 'https://www.invesco.com/us/en/financial-products/etfs/invesco-qqq-trust-series-1.html'
QQQ_API = 'https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/46090E103'
INTEL_RESULTS = 'https://www.intc.com/financial-info/financial-results'
CIRCLE_Q2_2026 = "https://s206.q4cdn.com/265218871/files/doc_news/2026/Aug/05/CIRCLE_Q2-2026.pdf"
CIRCLE_FY2025 = "https://s206.q4cdn.com/265218871/files/doc_earnings/2025/q4/earnings-result/4Q25-Earnings-Release.pdf"
SOXX_SPLIT_PDF = "https://www.ishares.com/us/literature/summary-prospectus/sp-ishares-phlx-semiconductor-etf-3-31.pdf"
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
            (url == QQQ_PAGE) or
            (p.hostname=='dng-api.invesco.com' and p.path in {
                '/cache/v1/accounts/en_US/shareclasses/46090E103',
                '/cache/v1/accounts/en_US/shareclasses/46090E103/navs',
                '/cache/v1/accounts/en_US/shareclasses/46090E103/prices',
                '/cache/v1/accounts/en_US/shareclasses/46090E103/holdings/fund'}) or
            (p.hostname=='www.intc.com' and (url==INTEL_RESULTS or p.path.startswith('/news-events/press-releases/detail/'))) or
            (p.hostname == "data.sec.gov" and any(p.path in {
                f"/submissions/CIK{cik}.json", f"/api/xbrl/companyfacts/CIK{cik}.json"}
                for cik in SEC_ISSUERS.values())) or
            (p.hostname == 'www.sec.gov' and any(p.path.startswith('/Archives/edgar/data/'+str(int(cik))+'/')
                for cik in SEC_ISSUERS.values()) and p.path.endswith(('.htm','.html'))) or
            (url == GOOGLE_10Q_2026Q2) or
            (url == GOOGLE_SUBMISSIONS) or
            (p.hostname=='www.sec.gov' and p.path.startswith('/Archives/edgar/data/1652044/') and p.path.endswith(('.htm','.html'))) or
            (p.hostname == "s206.q4cdn.com" and p.path.startswith("/479360582/files/doc_financials/")) or
            (p.hostname == "s206.q4cdn.com" and p.path.startswith("/265218871/files/")) or
            (p.hostname == "investor.circle.com" and p.path == "/overview/default.aspx") or
            (p.hostname == "www.ishares.com" and p.path == "/us/literature/summary-prospectus/sp-ishares-phlx-semiconductor-etf-3-31.pdf") or
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
    try:_google_class_filing(store,out,now,transport)
    except Exception as exc:
        _gap(out['primary_gaps'],str(exc) if isinstance(exc,EvidenceError) else 'CLASS_FILING_UNAVAILABLE',
             out['symbol']+'_class_filing',GOOGLE_10Q_2026Q2)


def _google_class_filing(store,out,now,transport):
    source=None;published=None;period='2026-06-30';year=2026;q=2
    try:
        listing=_fetch(store,GOOGLE_SUBMISSIONS,now,transport)
        recent=json.loads(listing['content'])['filings']['recent']
        for i,form in enumerate(recent['form']):
            if form!='10-Q':continue
            candidate_period=recent['reportDate'][i]
            candidate_pub=(recent.get('acceptanceDateTime') or [])[i] if recent.get('acceptanceDateTime') else _published(recent['filingDate'][i])
            if candidate_period>str(parse_time(now).date()) or parse_time(candidate_pub)>parse_time(now):continue
            accession=recent['accessionNumber'][i].replace('-','');document=recent['primaryDocument'][i]
            if not re.fullmatch(r'\d{18}',accession) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.html?',document):raise EvidenceError('FILING_INDEX_INVALID')
            url=f'https://www.sec.gov/Archives/edgar/data/1652044/{accession}/{document}'
            source=_fetch(store,url,now,transport);period=candidate_period;published=candidate_pub
            year=int(period[:4]);q=int(period[5:7])//3
            break
    except Exception:
        out.setdefault('primary_collection',{}).update(retry_required=True,class_filing_discovery='retry_latest_SEC_index')
    if source is None:source=_fetch(store,GOOGLE_10Q_2026Q2,now,transport)
    text=source['content']
    month={1:'March',2:'June',3:'September'}.get(q)
    if not month:raise EvidenceError('SOURCE_PERIOD_MISMATCH')
    if not ('Alphabet Inc.' in text and '0001652044' in text and 'FORM 10-Q' in text
            and f'For the quarterly period ended {month} {int(period[-2:])}, {year}' in text):
        raise EvidenceError('CLASS_FILING_ISSUER_PERIOD_MISMATCH')
    if published is None:published=_published(_line(text,r'Form:\s*10-Q,\s*Received:\s*(\d{2}/\d{2}/\d{4})').group(1))
    if parse_time(published)>parse_time(now):raise EvidenceError('SOURCE_NOT_AVAILABLE_AS_OF')
    start=text.index('Note 11. Stockholders');end=text.index('Note 13.',start)
    excerpt=text[start:end]
    quarter=excerpt.split('Note 12.',1)[1]
    quarter=re.split(r'(?:Six|Nine) Months Ended',quarter,maxsplit=1)[0]
    if not re.search(r'Three Months Ended '+month+r' '+period[-2:]+r',\s*'+str(year-1)+r'\s*'+str(year),quarter):raise EvidenceError('SOURCE_PERIOD_MISMATCH')
    if 'Class AClass BClass CConsolidatedClass AClass BClass CConsolidated' not in quarter.replace(' ','').replace('\n',''):
        # Whitespace-insensitive comparison, without guessing a reordered table.
        expected='ClassAClassBClassCConsolidatedClassAClassBClassCConsolidated'
        if expected not in re.sub(r'\s+','',quarter):raise EvidenceError('CLASS_FILING_COLUMN_ORDER_CHANGED')
    diluted=quarter.split('Diluted net income per common share:',1)[1]
    eps,eps_span=_circle_row(diluted,r'Diluted net income per common share\b',8)
    shares,shares_span=_circle_row(diluted,r'Number of shares used in per share computation',8)
    numerator,num_span=_circle_row(diluted,r'(?m)^Net income\b',8)
    index=6 if out['symbol']=='GOOG' else 4;share_class='Class C' if index==6 else 'Class A'
    if abs(numerator[index]/shares[index]-eps[index])>Decimal('.01'):raise EvidenceError('CLASS_FILING_EPS_RECONCILIATION_FAILED')
    if 'pursuant to the if-converted method' not in excerpt or 'Preferred dividends are added back' not in excerpt:
        raise EvidenceError('CLASS_FILING_PREFERRED_METHOD_MISSING')
    selected=copy.deepcopy(source);selected.update(content=excerpt,sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        parser=VERSION+':alphabet-notes-11-12')
    ev=_evidence(store,selected,published_at=published,period_end=period);out['primary_evidence'].append(ev)
    pf=out['primary_financials']
    for metric,value,span,unit in [('class_diluted_eps',eps[index],eps_span,'per_share'),
                                  ('class_diluted_shares',shares[index]*10**6,shares_span,None),
                                  ('class_diluted_numerator',numerator[index]*10**6,num_span,None)]:
        fact=_fact(metric,value,ev,span,year=year,quarter=q,price_unit=unit,locator='10-Q Note 12; current quarterly class-specific columns; USD millions except EPS')
        fact.update(eps_scope=share_class,symbol=out['symbol']);pf['facts'].append(fact)
    pf['class_eps_reconciliation']={'status':'verified','requested_symbol':out['symbol'],'share_class':share_class,
        'period_end':period,'diluted_eps':str(eps[index]),'diluted_shares':str(shares[index]*10**6),
        'group_diluted_shares':str(shares[7]*10**6),'evidence_refs':[ev['id']],
        'source_excerpt':eps_span+'\n'+shares_span+'\n'+num_span,
        'limitation':'Historical class-specific data only; future class EPS still requires an explicit scenario and allocation assumptions.'}
    pf['financing_reconciliation']={'status':'verified_historical_method','evidence_refs':[ev['id']],
        'source_excerpt':excerpt,'preferred_method':'if-converted: add back preferred dividends when including assumed converted shares; never deduct dividends a second time',
        'warning':'Separate actual current shares, weighted-average diluted shares, future scenario dilution and capped-call offsets. Do not extrapolate stub-quarter preferred dividends as a full coupon.'}
    if all(value in excerpt for value in ('May 15, 2029','2.2740','2.8420')):
        pf['financing_reconciliation'].update(conversion_date='2029-05-15',series_b_class_c_conversion_range=['2.2740','2.8420'])


def _circle_row(text, label, count=4):
    """Read a complete explicit column row; return its unmodified source span."""
    match = re.search(label + r"(?P<values>(?:\s*\$?\s*" + NUMBER + r"){" + str(count) + r"})(?![\d,.])", text, re.I)
    if not match:
        raise EvidenceError("SOURCE_TABLE_LAYOUT_CHANGED")
    tokens = re.findall(NUMBER, match.group("values"))
    if len(tokens) != count:
        raise EvidenceError("SOURCE_TABLE_LAYOUT_CHANGED")
    values = [decimal_value(token.replace(",", "").replace("(", "-").replace(")", "")) for token in tokens]
    return values, match.group()


def _circle_release(store, source, now, year, quarter):
    """Parse issuer GAAP statements and separately labelled non-GAAP reconciliation."""
    text = source["content"]
    if not re.search(r"Circle\s+Internet\s+Group,\s*Inc\.\s*\(NYSE:\s*CRCL\)", text, re.I):
        raise EvidenceError("SOURCE_ISSUER_OR_UNIT_MISMATCH")
    published = _published(_line(text, DATE).group())
    if parse_time(published) > parse_time(now):
        raise EvidenceError("SOURCE_NOT_AVAILABLE_AS_OF")
    period = f"{year}-{quarter * 3:02d}-" + ("30" if quarter in {2, 3} else "31")
    statement = re.search(r"CONSOLIDATED\s+STATEMENTS\s+OF\s+OPERATIONS(?P<table>.*?)Weighted.average\s+(?:common\s+)?shares\s+used", text, re.I | re.S)
    if not statement:
        raise EvidenceError("SOURCE_TABLE_LAYOUT_CHANGED")
    table = statement.group()
    unit = re.search(r"\(in\s+(?:\$\s*)?thousands,\s*except\s+per\s+share\s+information\)", table, re.I)
    if not unit or not re.search(r"Three\s+Months\s+Ended", table, re.I):
        raise EvidenceError("SOURCE_ISSUER_OR_UNIT_MISMATCH")
    if quarter == 4:
        if not re.search(r"Year\s+Ended", table, re.I):
            raise EvidenceError("SOURCE_PERIOD_MISMATCH")
    elif not re.search(r"Six\s+Months\s+Ended", table, re.I) and quarter == 2:
        raise EvidenceError("SOURCE_PERIOD_MISMATCH")
    # Column order is verified from all four headings. YTD is not full-year EPS.
    header = re.split(r"Revenue\s+and\s+reserve\s+income", table, maxsplit=1, flags=re.I)[0]
    dates = [_date(m.group()) for m in re.finditer(DATE, header, re.I)]
    previous = str(year - 1) + period[4:]
    if dates != [period, previous, period, previous]:
        raise EvidenceError("SOURCE_PERIOD_MISMATCH")
    evidence = _evidence(store, source, published_at=published, period_end=period)
    facts = []
    eps_part = re.search(r"Earnings\s+\(loss\)\s+per\s+share\s+attributable\s+to\s+common\s+stockholders\s*:(.*)", table, re.I | re.S)
    if not eps_part:
        raise EvidenceError("SOURCE_TABLE_LAYOUT_CHANGED")
    long_eps = r"Diluted\s+earnings\s+\(loss\)\s+per\s+common\s+share\s+attributable\s+to\s+common\s+stockholders\s*(?!:)"
    eps, eps_excerpt = _circle_row(eps_part.group(1), long_eps if quarter == 4 else r"Diluted\b")
    specs = [("eps_diluted", eps, eps_excerpt, "per_share", Decimal(1)),
        ("revenue", *_circle_row(table, r"Total\s+revenue\s+and\s+reserve\s+income"), None, Decimal(1000)),
        ("reserve_income", *_circle_row(table, r"Reserve\s+income"), None, Decimal(1000)),
        ("operating_income", *_circle_row(table, r"Operating\s+income\s+\(loss\)\s+from\s+continuing\s+operations"), None, Decimal(1000)),
        ("net_income_continuing_operations", *_circle_row(table, r"Net\s+income\s+\(loss\)\s+from\s+continuing\s+operations\b(?!\s+before)"), None, Decimal(1000))]
    for metric, values, excerpt, price_unit, scale in specs:
        for index, q in ((0, quarter), (2, None)) if quarter == 4 else ((0, quarter),):
            fact = _fact(metric, values[index] * scale, evidence, excerpt, year=year, quarter=q,
                price_unit=price_unit, locator="Circle issuer consolidated GAAP statement; dollar thousands except per-share; explicit current/prior quarter and YTD columns")
            fact.update(accounting_basis="GAAP", source_unit_label=unit.group(),
                currency_basis="Circle US issuer dollar financial-statement presentation; no currency conversion")
            if metric == "eps_diluted":
                fact.update(eps_scope="consolidated_common_stockholders",
                    source_rationale="Reported diluted common EPS, not adjusted EPS, a consensus forecast or annualized quarterly earnings.")
            facts.append(fact)
    instrument = {"is_adr": False, "quote_currency": "USD", "quote_price_unit": "per_share",
        "stock_eps_valuation_applicable": True, "evidence_refs": [evidence["id"]],
        "source_locator": "Circle issuer NYSE CRCL heading and reported diluted common EPS statement",
        "source_excerpt": eps_part.group(), "unit_source_excerpt": unit.group()}
    adjusted = []
    # Reconciliation row, never the rounded headline or an EPS substitute. The
    # FY25 definition differs from Q2'26; retain the issuer's exact label.
    if quarter == 2:
        pattern = r"Adjusted\s+EBITDA\s*\(New\s+De(?:fi|\ufb01)nition\)"
        try:
            values, excerpt = _circle_row(text, pattern, count=5)
            fact = _fact("adjusted_ebitda", values[0] * 1000, evidence, excerpt,
                year=year, quarter=quarter, locator="issuer reconciliation to GAAP net income; USD thousands; New Definition")
            fact.update(accounting_basis="non_GAAP", definition="issuer new definition; includes exclusion of stock-based compensation related payroll expense",
                warning="Not GAAP net income or EPS; changes in issuer definition impair comparability.")
            adjusted.append(fact)
        except EvidenceError:
            pass  # Missing non-GAAP row is explicit below, never replace GAAP.
    elif quarter == 4:
        reconciliation = re.search(r"\(in\s+\$\s+thousands\)\s+Year\s+Ended\s+December\s+31,\s*" + str(year) + r".*?Adjusted\s+EBITDA\s*\$", text, re.I | re.S)
        if reconciliation:
            try:
                tail = text[reconciliation.start():]
                values, excerpt = _circle_row(tail, r"Adjusted\s+EBITDA", count=2)
                fact = _fact("adjusted_ebitda", values[0] * 1000, evidence, excerpt,
                    year=year, locator="issuer annual reconciliation to GAAP net income; USD thousands")
                fact.update(accounting_basis="non_GAAP", definition="issuer definition in FY25 release",
                    warning="Not GAAP net income or EPS; do not substitute for reported annual diluted EPS.")
                adjusted.append(fact)
            except EvidenceError:
                pass
    return evidence, facts, instrument, adjusted


def _circle(store, out, now, transport):
    # IR discovery is bounded to its latest earnings-release link. A release
    # announcement/presentation or a future-dated document is never a result.
    targets, discovery_ok, discovery_gaps = [], False, []
    try:
        page = _fetch(store, CIRCLE_IR, now, transport)
        parser = _HTML()
        parser.feed(base64.b64decode(page["raw_base64"]).decode("utf-8-sig"))
        for href, title in parser.links:
            match = re.search(r"Earnings\s+Press\s+Release\s+of\s+Q([1-4])\s+(\d{4})", title, re.I)
            if match:
                url = urljoin(CIRCLE_IR, href)
                if not _allowed(url) or not urlsplit(url).path.lower().endswith(".pdf"):
                    raise EvidenceError("SOURCE_URL_NOT_ALLOWED")
                targets.append((int(match[2]), int(match[1]), url))
        if not targets:
            raise EvidenceError("SOURCE_LATEST_RELEASE_LINK_UNAVAILABLE")
        targets = sorted(set(targets), reverse=True)[:1]
        discovery_ok = True
    except Exception as exc:
        _gap(discovery_gaps, str(exc) if isinstance(exc, EvidenceError) else "PRIMARY_SOURCE_UNAVAILABLE", "CRCL_LATEST_RELEASE_DISCOVERY", CIRCLE_IR)
    # Exact original public documents independently located through Circle IR.
    # They are dated historical fallbacks, not a claim to be the newest release.
    targets.extend([(2026, 2, CIRCLE_Q2_2026), (2025, 4, CIRCLE_FY2025)])
    seen, latest = set(), None
    for year, quarter, url in targets:
        if url in seen:
            continue
        seen.add(url)
        try:
            source = _fetch(store, url, now, transport)
            evidence, facts, instrument, adjusted = _circle_release(store, source, now, year, quarter)
            out["primary_evidence"].append(evidence)
            existing = {(f["metric"], f["period_end"], f["period_type"]) for f in out["primary_financials"]["facts"]}
            out["primary_financials"]["facts"].extend(f for f in facts if (f["metric"], f["period_end"], f["period_type"]) not in existing)
            out["primary_financials"].setdefault("non_gaap_facts", []).extend(adjusted)
            if latest is None or evidence["period_end"] > latest:
                latest = evidence["period_end"]
                out["primary_financials"]["instrument"] = instrument
            if not adjusted:
                _gap(out["primary_gaps"], "NON_GAAP_RECONCILIATION_NOT_PARSED", "CRCL_NON_GAAP", url)
                out["primary_gaps"][-1]["blocking"] = False
        except Exception as exc:
            _gap(out["primary_gaps"], str(exc) if isinstance(exc, EvidenceError) else "PRIMARY_SOURCE_UNAVAILABLE", f"CRCL_{year}Q{quarter}", url)
    out["primary_financials"].update(latest_reported_period_end=latest,
        eps_scope="consolidated_common_stockholders",
        annual_forecast_verified=False,
        forecast_limitation="Issuer historical GAAP statements only. Quarterly or year-to-date EPS is never multiplied into a full-year consensus forecast.")
    # Discovery health belongs to collection audit, never to the material facts
    # digest. A transient IR outage must not manufacture a new paid review.
    out["primary_collection"] = {"status": "issuer_release_discovered" if discovery_ok else "dated_historical_fallback",
        "attempted_at": now, "gaps": discovery_gaps, "retry_required": not discovery_ok}
    cached = (store.get("fundamental_cache", "CRCL") or {}).get("data") or {}
    previous = cached.get("primary_financials") or {}
    previous_latest = previous.get("latest_reported_period_end")
    if previous_latest and (latest is None or previous_latest > latest):
        from .market import _primary_verified
        old_facts = previous.get("facts") or []
        if old_facts and all(_primary_verified(store, fact) for fact in old_facts):
            out["primary_collection"].update(status="retained_last_known_evidence", retry_required=True,
                gaps=discovery_gaps + out["primary_gaps"], retained_period_end=previous_latest)
            out["primary_financials"] = copy.deepcopy(previous)
            for fact in out["primary_financials"]["facts"]:
                fact.pop("source_verified", None)
            out["primary_financials"].get("instrument", {}).pop("source_verified", None)
            out["primary_evidence"] = copy.deepcopy(cached.get("primary_evidence", []))
            out["primary_gaps"] = copy.deepcopy(cached.get("primary_gaps", []))


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


def _soxx_split_supplement(store, out, now, transport):
    """Issuer announcement only: no ledger split, position or price conversion."""
    valuation = out["etf_valuation"]
    try:
        source = _fetch(store, SOXX_SPLIT_PDF, now, transport)
        text = source["content"]
        sections = list(re.finditer(r"Supplement\s+dated\s+(?P<date>" + DATE
            + r")(?P<body>.*?)PLEASE\s+RETAIN\s+THIS\s+SUPPLEMENT", text, re.I | re.S))
        if not sections:
            raise EvidenceError("SOURCE_SPLIT_SUPPLEMENT_LAYOUT_CHANGED")
        published = max(_published(m["date"]) for m in sections)
        # The changing PDF can include a later confirming supplement. Never
        # date the entire bytes/text bundle as the older announcement alone.
        if parse_time(published) > parse_time(now):
            raise EvidenceError("SOURCE_NOT_AVAILABLE_AS_OF")
        parsed = []
        for section in sections:
            body = section["body"]
            if not re.search(r"iShares\s+Semiconductor\s+ETF\s*\(SOXX\)", body):
                continue
            ratio = re.search(r"iShares\s+Semiconductor\s+ETF\s+SOXX\s+(\d+)\s*:\s*(\d+)", body)
            words = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)-for-(one|two|three|four|five|six|seven|eight|nine|ten)\s+forward\s+stock\s+split", body, re.I)
            if ratio:
                numerator, denominator = int(ratio[1]), int(ratio[2])
            elif words:
                numbers = {word: index for index, word in enumerate(("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"), 1)}
                numerator, denominator = numbers[words[1].lower()], numbers[words[2].lower()]
            else:
                raise EvidenceError("SOURCE_SPLIT_RATIO_UNAVAILABLE")
            record = re.search(r"record\s+date\s+for\s+the\s+(?:forward\s+stock\s+)?split\s+will\s+be\s+(" + DATE + r")", body, re.I)
            distribution = re.search(r"(?:distribution\s+date\s+will\s+be|effectuated\s+after\s+the\s+close\s+of\s+trading\s+on)\s+(" + DATE + r")", body, re.I)
            effective = re.search(r"(?:split-adjusted\s+basis\s+on|Nasdaq\s+Stock\s+Market\s+LLC\s+on)\s+(" + DATE + r")", body, re.I)
            if not (record and distribution and effective and numerator > denominator > 0):
                raise EvidenceError("SOURCE_SPLIT_DATES_OR_RATIO_UNAVAILABLE")
            days = (_date(record[1]), _date(distribution[1]), _date(effective[1]))
            if not (_date(section["date"]) <= days[0] <= days[1] < days[2]):
                raise EvidenceError("SOURCE_SPLIT_DATE_CONFLICT")
            parsed.append((section, numerator, denominator, days))
        if not parsed or len({(n, d, days) for _,n,d,days in parsed}) != 1:
            raise EvidenceError("SOURCE_SPLIT_SUPPLEMENTS_CONFLICT")
        # Prefer the original numeric table's full literal span. It also states
        # the split-adjusted trading date consumed by the existing price gate.
        section, numerator, denominator, days = next((item for item in parsed
            if re.search(r"\bSOXX\s+\d+\s*:\s*\d+", item[0].group())), parsed[0])
        evidence = _evidence(store, source, published_at=published, period_end=days[2])
        out["primary_evidence"].append(evidence)
        action = {"type": "split", "announced_at": min(_date(m["date"]) for m,_,_,_ in parsed),
            "source_published_at": published, "confirmed_at": published, "record_date": days[0],
            "distribution_date": days[1], "effective_at": days[2],
            "status": "announced_not_effective" if days[2] > parse_time(now).date().isoformat() else "effective_requires_unit_check",
            "ratio": f"{numerator}:{denominator}", "new_shares_per_old": str(Decimal(numerator) / Decimal(denominator)),
            "evidence_refs": [evidence["id"]], "source_excerpt": section.group(), "source_url": SOXX_SPLIT_PDF,
            "source_locator": "issuer dated split supplements; record/distribution/split-adjusted trading dates",
            "automatic_price_adjustment": False, "ledger_confirmation_required": True}
        existing = valuation["corporate_actions"]
        if any(old.get("type") == "split" and old.get("effective_at") != days[2] for old in existing):
            raise EvidenceError("SOURCE_SPLIT_ANNOUNCEMENT_CONFLICT")
        valuation["corporate_actions"] = [old for old in existing if old.get("type") != "split"] + [action]
    except Exception as exc:
        code = str(exc) if isinstance(exc, EvidenceError) else "PRIMARY_SOURCE_UNAVAILABLE"
        if code in {"SOURCE_SPLIT_SUPPLEMENTS_CONFLICT", "SOURCE_SPLIT_ANNOUNCEMENT_CONFLICT", "SOURCE_SPLIT_DATE_CONFLICT"}:
            # A new contradictory issuer disclosure is material evidence, not
            # a collection outage. Keep its original text/hash visible to the
            # research gate; restoring the cached ratio would hide the change.
            evidence = _evidence(store, source, published_at=published)
            out["primary_evidence"].append(evidence)
            valuation["corporate_action_conflict"] = True
            valuation["corporate_action_conflict_evidence_refs"] = [evidence["id"]]
            _gap(out["primary_gaps"], code, "SOXX_SPLIT_SUPPLEMENT", SOXX_SPLIT_PDF)
            return
        # A temporary failed refresh cannot erase previously sealed ratio/date
        # evidence. The portfolio itself is never transformed by this collector.
        cached = (store.get("fundamental_cache", "SOXX") or {}).get("data") or {}
        from .market import _primary_verified
        kept = []
        for old in (cached.get("etf_valuation") or {}).get("corporate_actions", []):
            try:
                published_before_cutoff = parse_time(old.get("source_published_at")) <= parse_time(now)
            except (TypeError, ValueError):
                published_before_cutoff = False
            if (old.get("type") == "split" and old.get("new_shares_per_old")
                    and _primary_verified(store, old)
                    and published_before_cutoff
                    and not any(current.get("type") == "split" and current.get("effective_at") != old.get("effective_at")
                                for current in valuation["corporate_actions"])):
                retained = copy.deepcopy(old)
                retained["status"] = ("announced_not_effective" if old["effective_at"] > parse_time(now).date().isoformat()
                                      else "effective_requires_unit_check")
                valuation["corporate_actions"] = [item for item in valuation["corporate_actions"] if item.get("type") != "split"] + [retained]
                kept.extend(old["evidence_refs"])
        for ref in kept:
            record = store.get("external_evidence", ref)
            out["primary_evidence"].append({key: record[key] for key in
                ("id", "source_url", "sha256", "raw_sha256", "published_at", "period_end", "observed_at")})
        out["primary_collection"] = {"status": "split_supplement_retained" if kept else "split_supplement_unavailable",
            "attempted_at": now, "retry_required": True, "gaps": [{"code": code, "field": "SOXX_SPLIT_SUPPLEMENT", "source_url": SOXX_SPLIT_PDF}]}


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
    _soxx_split_supplement(store, out, now, transport)
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


def _qqq(store, out, now, transport):
    source = _fetch(store, QQQ_PAGE, now, transport)
    title = 'Invesco QQQ Trust, Series 1'
    share_text = 'ETF Shares are not individually redeemable'
    if title not in source['content'] or share_text not in source['content']:
        raise EvidenceError('QQQ_ISSUER_PAGE_IDENTITY_CHANGED')
    ev = _evidence(store, source)
    out['primary_evidence'].append(ev)
    out['primary_financials']['instrument'] = dict(asset_type='etf', stock_eps_valuation_applicable=False,
        evidence_refs=[ev['id']], source_excerpt=title, unit_source_excerpt=share_text,
        source_locator='Official Invesco QQQ issuer page and ETF shares disclosure')
    _qqq_data(store,out,source,now,transport)


def _json_evidence(store,source,value,*,period_end=None,published_at=None):
    content=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    selected=dict(source,content=content,sha256=hashlib.sha256(content.encode()).hexdigest(),
        parser=VERSION+':selected-json-v1')
    return _evidence(store,selected,period_end=period_end,published_at=published_at),content


def _qqq_data(store,out,page,now,transport):
    """Issuer-published month-end valuation and separately dated daily risk data.

    The endpoints/CUSIP/locale are declared by the issuer page itself. Historical
    NAV is paired with the P/E date; newer holdings are risk context, not a
    fabricated same-day historical portfolio or a new company EPS estimate.
    """
    paths={'pe':'?expand=nav&idType=cusip&variationType=fundCharacteristics&productType=ETF',
        'navs':'/navs?idType=cusip&productType=ETF',
        'prices':'/prices?idType=cusip&variationType=priceListing&productType=ETF',
        'holdings':'/holdings/fund?idType=cusip&productType=ETF'}
    sources={k:_fetch(store,QQQ_API+path,now,transport) for k,path in paths.items()}
    values={k:json.loads(v['content']) for k,v in sources.items()}
    if any(v.get('cusip')!='46090E103' for v in values.values()):raise EvidenceError('QQQ_CUSIP_MISMATCH')
    if values['pe'].get('currencyCode')!='USD' or values['navs'].get('currency')!='USD' or values['prices'].get('currency')!='USD':
        raise EvidenceError('QQQ_CURRENCY_MISMATCH')
    pe_day=_date(values['pe']['effectiveDate'])
    if not _available_day(pe_day,now):raise EvidenceError('SOURCE_NOT_AVAILABLE_AS_OF')
    nav_rows=next(v['data'] for v in values['navs']['lineChartData'] if v.get('type')=='NAV')
    dated=[(_date(v['date']),v) for v in nav_rows if _available_day(_date(v['date']),now)]
    baseline=next(v for day,v in dated if day==pe_day)
    latest_day,latest=max(dated,key=lambda v:v[0])
    price_day=_date(values['prices']['effectiveDate']);holding_day=_date(values['holdings']['effectiveDate'])
    if not all(_available_day(day,now) for day in (price_day,holding_day)):raise EvidenceError('SOURCE_NOT_AVAILABLE_AS_OF')
    # Validate the definition of USD NAV per ETF share from issuer assets/shares.
    prices=values['prices']
    if (decimal_value(prices['sharesOutstanding'])<=0 or decimal_value(prices['nav'])<=0
            or abs(decimal_value(prices['marketValue'])/decimal_value(prices['sharesOutstanding'])-decimal_value(prices['nav']))>Decimal('.00001')):
        raise EvidenceError('QQQ_NAV_SHARE_RECONCILIATION_FAILED')
    decoded=unescape(base64.b64decode(page['raw_base64']).decode('utf-8-sig')).replace('\\"','"')
    maps=[]
    for match in re.finditer(r'"productListFieldValueMap"\s*:\s*(\{[^{}]*\})',decoded):
        row=json.loads(match.group(1))
        if row.get('ticker')=='QQQ' and row.get('cusip')=='46090E103':maps.append(row)
    if not maps or len({str(m.get('totalExpenseRatio')) for m in maps})!=1:raise EvidenceError('QQQ_FEE_METADATA_MISSING_OR_CONFLICTING')
    fee=maps[0];ratio=decimal_value(fee['totalExpenseRatio'])
    if not 0<=ratio<2:raise EvidenceError('QQQ_FEE_OUTSIDE_VALID_RANGE')
    evs={};texts={}
    for key,val,day in [('pe',values['pe'],pe_day),('navs',{'cusip':'46090E103','currency':'USD','baseline':baseline,'latest':latest},latest_day),
                         ('prices',prices,price_day),('holdings',values['holdings'],holding_day),('fee',fee,None)]:
        evs[key],texts[key]=_json_evidence(store,page if key=='fee' else sources[key],val,period_end=day)
        out['primary_evidence'].append(evs[key])
    def part(key,**fields):return dict(fields,evidence_refs=[evs[key]['id']],source_url=evs[key]['source_url'],source_excerpt=texts[key])
    holdings=[];unweighted=[]
    for i,row in enumerate(values['holdings']['holdings']):
        if row['percentageOfTotalNetAssets'] is None:
            if row.get('securityTypeName')!='Currency' or row.get('ticker')!='USDPDV':raise EvidenceError('QQQ_HOLDINGS_WEIGHT_MISSING')
            unweighted.append(row)
            continue  # Pending dividend row is disclosed, never imputed as an equity weight.
        weight=decimal_value(row['percentageOfTotalNetAssets'])/100
        if not 0<=weight<=1 and not (row.get('securityTypeName')=='Synthetic Cash' and Decimal('-.02')<=weight<0):
            raise EvidenceError('QQQ_HOLDINGS_WEIGHT_INVALID')
        holdings.append({'symbol':row.get('ticker') or 'CASH_'+str(i),'weight':str(weight),
            'name':row.get('issuerName'),'security_type':row.get('securityTypeName')})
    total=sum(decimal_value(r['weight']) for r in holdings)
    if not holdings or not Decimal('.95')<=total<=Decimal('1.01'):raise EvidenceError('QQQ_HOLDINGS_COVERAGE_INCOMPLETE')
    out['etf_valuation']={'issuer_verified':True,'currency':'USD','price_unit':'per_share',
        'timing_contract':'issuer_month_end_with_daily_risk_v1',
        'nav':part('navs',value=str(decimal_value(baseline['value'])),as_of=pe_day,currency='USD',price_unit='per_share'),
        'pe':part('pe',value=str(decimal_value(values['pe']['priceToEarningsRatio'])),as_of=pe_day,
            methodology='Issuer month-end weighted harmonic portfolio P/E; excludes negative earnings per issuer methodology; implied ETF earnings proxy, not reported corporate EPS'),
        'latest_nav':part('prices',value=str(decimal_value(prices['nav'])),as_of=price_day,currency='USD',price_unit='per_share'),
        'holdings':part('holdings',as_of=holding_day,rows=holdings,coverage=str(total),unweighted_disclosures=unweighted,
            concentration={'top10_weight':str(sum(sorted((decimal_value(r['weight']) for r in holdings),reverse=True)[:10]))}),
        'expense_ratio':part('fee',value=str(ratio),unit='percent_per_year'),
        'corporate_actions':[],
        'timing_disclosure':'NAV and P/E share the exact month-end date. Newer holdings describe current concentration risk only. Latest daily NAV is a separate execution premium gate; no dates are silently shifted.'}
    out['primary_financials']['instrument']=dict(is_adr=False,asset_type='etf',quote_currency='USD',quote_price_unit='per_share',
        stock_eps_valuation_applicable=False,evidence_refs=[evs['prices']['id']],source_excerpt=texts['prices'],
        source_locator='Invesco /prices: CUSIP, USD NAV, net assets and ETF shares reconcile; fund is identified by official issuer page')


def _sec_equity(store, out, now, transport):
    """Select dated SEC facts, never annualize YTD or infer a share-class split.

    Raw responses remain immutable; the model receives only the selected original
    JSON rows and their explicit units, accession and original period boundaries.
    """
    symbol = out['symbol']; cik = SEC_ISSUERS[symbol]; moment = parse_time(now)
    listing = _fetch(store, f'https://data.sec.gov/submissions/CIK{cik}.json', now, transport)
    submission = json.loads(listing['content'])
    if (int(submission.get('cik', -1)) != int(cik)
            or symbol not in submission.get('tickers', [])
            or submission.get('exchanges', [])[submission['tickers'].index(symbol)] != 'Nasdaq'):
        raise EvidenceError('SEC_INSTRUMENT_IDENTITY_MISMATCH')
    source = _fetch(store, f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json', now, transport)
    data = json.loads(source['content'])
    if int(data.get('cik', -1)) != int(cik):
        raise EvidenceError('SEC_FACTS_ISSUER_MISMATCH')
    recent = submission['filings']['recent']
    accepted = {a: t for a, t in zip(recent['accessionNumber'], recent['acceptanceDateTime'])}
    fiscal_ends={day for i,(form,day) in enumerate(zip(recent.get('form',[]),recent.get('reportDate',[])))
        if form in {'10-K','10-K/A'} and re.fullmatch(r'\d{4}-\d{2}-\d{2}',day or '')
        and parse_time(recent['acceptanceDateTime'][i])<=moment}
    tags = [('eps_diluted', ['EarningsPerShareDiluted'], 'USD/shares'),
            ('diluted_shares', ['WeightedAverageNumberOfDilutedSharesOutstanding'], 'shares'),
            ('net_income', ['NetIncomeLoss'], 'USD'),
            ('operating_income', ['OperatingIncomeLoss'], 'USD'),
            ('revenue', ['RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'SalesRevenueNet'], 'USD'),
            ('income_tax_expense', ['IncomeTaxExpenseBenefit'], 'USD'),
            ('operating_cash_flow', ['NetCashProvidedByUsedInOperatingActivities'], 'USD'),
            ('capital_expenditure', ['PaymentsToAcquirePropertyPlantAndEquipment'], 'USD')]
    selected = []; gaap = data.get('facts', {}).get('us-gaap', {})
    for metric, alternatives, unit in tags:
        chosen = []
        for tag in alternatives:
            field = gaap.get(tag, {}); candidates = {}
            for row in field.get('units', {}).get(unit, []):
                try:
                    if row.get('form') not in {'10-Q', '10-K'}: continue
                    start, end = _date(row['start']), _date(row['end'])
                    publication = accepted.get(row['accn']) or _published(row['filed'])
                    days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
                    # A twelve-month observation ending in June is not Amazon's
                    # fiscal FY. Only actual 10-K report dates establish FY ends.
                    kind = ('annual' if end in fiscal_ends else 'trailing_twelve_months') if 330 <= days <= 380 else 'quarterly' if 60 <= days <= 110 else None
                    if kind=='trailing_twelve_months' and fiscal_ends and end<=max(fiscal_ends):continue
                    value = decimal_value(row['val'])
                    if (not kind or not value.is_finite() or parse_time(publication) > moment
                            or end > str(moment.date()) or start > end): continue
                    key = (kind, start, end)
                    if key not in candidates or publication > candidates[key]['published_at']:
                        candidates[key] = dict(metric=metric, tag=tag, label=field.get('label'), unit=unit,
                            row=row, published_at=publication, period_type=kind)
                except (KeyError, ValueError, TypeError):
                    continue
            for kind, limit in [('annual', 2), ('quarterly', 4), ('trailing_twelve_months',1)]:
                chosen.extend(sorted((v for v in candidates.values() if v['period_type'] == kind),
                    key=lambda v: (v['row']['end'], v['published_at']), reverse=True)[:limit])
            if chosen: break  # Do not double count alternate revenue taxonomy tags.
        selected.extend(chosen)
    eps = next((r for r in selected if r['metric'] == 'eps_diluted' and r['unit'] == 'USD/shares'), None)
    if not eps or 'per share' not in str(eps['label']).lower():
        raise EvidenceError('SEC_DILUTED_COMMON_EPS_UNIT_MISSING')
    identity = {k: submission[k] for k in ('cik', 'name', 'tickers', 'exchanges')}
    # Keep the source raw hash linked to the full stored response, but bound the
    # excerpt before evidence-bundle construction (companyfacts can exceed 4MB).
    excerpt = json.dumps({'identity': identity, 'entityName': data['entityName'], 'selected_us_gaap': selected},
        ensure_ascii=False, sort_keys=True)
    extracted = dict(source, content=excerpt, sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        parser=VERSION + ':sec-selected-facts-v1')
    latest_pub = max(r['published_at'] for r in selected)
    ev = _evidence(store, extracted, published_at=latest_pub, period_end=max(r['row']['end'] for r in selected))
    out['primary_evidence'].append(ev)
    for item in selected:
        row = item['row']; end = datetime.fromisoformat(row['end'])
        fiscal_year = end.year + (1 if symbol == 'MSFT' and end.month > 6 else 0)
        quarter = ((end.month - 1 - (6 if symbol == 'MSFT' else 0)) % 12) // 3 + 1
        out['primary_financials']['facts'].append(dict(metric=item['metric'], value=str(decimal_value(row['val'])),
            currency='USD' if item['unit'].startswith('USD') else None,
            price_unit='per_share' if item['unit'] == 'USD/shares' else None,
            unit=item['unit'], period_type=item['period_type'],
            fiscal_year=None if item['period_type']=='trailing_twelve_months' else fiscal_year,
            fiscal_period='FY' if item['period_type'] == 'annual' else 'TTM' if item['period_type']=='trailing_twelve_months' else f'Q{quarter}',
            period_start=row['start'], period_end=row['end'], as_of=row['end'],
            published_at=item['published_at'], value_kind='reported', evidence_refs=[ev['id']],
            source_url=ev['source_url'], source_locator='SEC us-gaap/' + item['tag'] + '/' + row['accn'],
            source_excerpt=json.dumps(item, ensure_ascii=False, sort_keys=True)))
    out['primary_financials']['instrument'] = dict(is_adr=False, quote_currency='USD',
        quote_price_unit='per_share', stock_eps_valuation_applicable=True, evidence_refs=[ev['id']],
        source_excerpt=json.dumps(eps, ensure_ascii=False, sort_keys=True),
        source_locator='SEC CIK/ticker/exchange identity and reported diluted EPS USD/shares unit')
    out['primary_financials']['limitations'] = [
        'Reported GAAP history only; not a forward earnings forecast or verification of non-GAAP adjustments.',
        'No quarterly cash-flow values are inferred from year-to-date figures; fiscal dates are preserved.']
    # Earnings releases can precede the tagged 10-Q by a day or more. Discover
    # Item 2.02 filings; don't present old XBRL history as the latest announcement.
    for i, form in enumerate(recent.get('form', [])):
        if form != '8-K' or '2.02' not in recent.get('items', [''] * len(recent['form']))[i]: continue
        publication = recent['acceptanceDateTime'][i]
        if parse_time(publication) > moment or (symbol!='AMZN' and parse_time(publication) <= parse_time(latest_pub)): continue
        accession = recent['accessionNumber'][i].replace('-', '')
        document = recent['primaryDocument'][i]
        if not re.fullmatch(r'\d{18}', accession) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.html?', document):
            raise EvidenceError('SEC_EARNINGS_FILING_INDEX_INVALID')
        url=f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{document}'
        try:
            filing=_fetch(store,url,now,transport)
            parser=_HTML();parser.feed(base64.b64decode(filing['raw_base64']).decode('utf-8-sig'))
            links=[urljoin(url,href) for href,label in parser.links
                if re.search(r'99[._-]?1|99\.1',href+' '+label,re.I) and _allowed(urljoin(url,href))]
            if not links:raise EvidenceError('SEC_EARNINGS_EXHIBIT_NOT_FOUND')
            release=_fetch(store,links[0],now,transport)
            ev=_evidence(store,release,published_at=publication)
            out['primary_evidence'].append(ev)
            out['primary_financials']['latest_earnings_release']={
                'published_at':publication,'evidence_refs':[ev['id']], 'source_url':ev['source_url'],
                'tagged_financial_rows_not_yet_available':parse_time(publication)>parse_time(latest_pub),
                'instruction':'Original earnings announcement; reconcile its period with tagged facts. Never label its future guidance as reported results.'}
        except Exception:
            recovered=False
            if symbol=='INTC':
                try:
                    index=_fetch(store,INTEL_RESULTS,now,transport)
                    parser=_HTML();parser.feed(base64.b64decode(index['raw_base64']).decode('utf-8-sig'))
                    links=[urljoin(INTEL_RESULTS,href) for href,label in parser.links
                        if label.strip().lower()=='earnings release' and _allowed(urljoin(INTEL_RESULTS,href))]
                    if not links:raise EvidenceError('INTEL_RELEASE_LINK_MISSING')
                    release=_fetch(store,links[0],now,transport)
                    if ('Intel Reports' not in release['content'] or 'Financial Results' not in release['content']
                            or _date(_line(release['content'],DATE).group()) != recent['filingDate'][i]):
                        raise EvidenceError('INTEL_RELEASE_DATE_MISMATCH')
                    ev=_evidence(store,release,published_at=publication)
                    out['primary_evidence'].append(ev)
                    out['primary_financials']['latest_earnings_release']={
                        'published_at':publication,'evidence_refs':[ev['id']], 'source_url':ev['source_url'],
                        'tagged_financial_rows_not_yet_available':True,
                        'instruction':'Official issuer announcement matched to the SEC Item 2.02 filing date; newer than tagged history. Separate GAAP, non-GAAP and forward guidance.'}
                    recovered=True
                except Exception:
                    pass
            if not recovered:
                _gap(out['primary_gaps'],'SEC_NEWER_EARNINGS_RELEASE_UNAVAILABLE','latest_earnings_release',url)
        break  # One latest announced quarter, at most two bounded document fetches.
    if symbol=='AMZN':
        _amazon_operating_basis(store,out,recent,now,transport)
    if (moment.date() - datetime.fromisoformat(max(r['row']['end'] for r in selected)).date()).days > 200:
        _gap(out['primary_gaps'], 'SEC_LATEST_PERIOD_STALE', symbol)


def _amazon_operating_basis(store,out,recent,now,transport):
    """Two original earnings announcements; exclude investment marks from EPS proxies."""
    latest=out['primary_financials'].get('latest_earnings_release') or {}
    refs=latest.get('evidence_refs') or []
    if not refs:return
    original=store.get('external_evidence',refs[0]);text=original['content']
    lines=text.splitlines()
    try:
        start=next(i for i,line in enumerate(lines) if 'Supplemental Financial Information' in line)
        end=next((i for i in range(start+1,len(lines)) if 'Supplemental Financial Information' in lines[i]),len(lines))
        section=lines[start:end]
        header=next(line for line in section if re.search(r'Q[1-4] 20\d{2}Q[1-4] 20\d{2}',line))
        quarters=re.findall(r'Q([1-4]) (20\d{2})',header)
        if len(quarters)!=6:raise EvidenceError('AMZN_TTM_COLUMNS_CHANGED')
        q,year=map(int,quarters[-1]);day=f'{year}-{q*3:02d}-'+('30' if q in (2,3) else '31')
        selected={}
        for metric,label in [('operating_income_ttm','Operating income -- TTM'),
                             ('operating_cash_flow_ttm','Operating cash flow -- trailing twelve months (TTM)'),
                             ('free_cash_flow_ttm','Free cash flow -- TTM (1)')]:
            # Issuer uses Unicode/nonbreaking dashes and parentheses in labels.
            words=label.split(' -- ')[0]
            candidates=[line for line in section if line.startswith(label+'$')]
            if len(candidates)!=1:raise EvidenceError('AMZN_TTM_ROW_AMBIGUOUS:'+metric)
            line=candidates[0];tail=line.split('$',1)[1]
            vals=re.findall(NUMBER,tail)
            if len(vals)<6:raise EvidenceError('AMZN_TTM_VALUES_CHANGED')
            selected[metric]={'value':str(decimal_value(vals[5].replace(',','').replace('(','-').replace(')',''))/1000),
                'unit':'USD_billions','source_excerpt':line,'column_header':header,'unit_source_excerpt':'(in millions, except per share data)',
                'scale_conversion':'reported millions / 1000 = billions','evidence_refs':refs,'as_of':day}
        index=next(i for i,line in enumerate(lines) if line.startswith('Weighted-average shares used in computation'))
        shareline=next(line for line in lines[index+1:index+5] if line.startswith('Diluted'))
        shares=re.findall(NUMBER,shareline)
        if len(shares)!=4:raise EvidenceError('AMZN_DILUTED_COLUMNS_CHANGED')
        selected['diluted_shares_quarter']={'value':str(decimal_value(shares[1].replace(',',''))/1000),
            'unit':'billions_of_shares','as_of':day,'evidence_refs':refs,
            'source_excerpt':shareline,'unit_source_excerpt':'(in millions, except per share data)',
            'column':'current three-month diluted weighted-average; second of four cells',
            'scale_conversion':'reported millions / 1000 = billions'}
        for item in selected.values():item.update(source_url=original['source_url'],published_at=original['published_at'])
        out['primary_financials']['operating_basis']=selected
    except (StopIteration,ValueError,IndexError) as error:
        _gap(out['primary_gaps'],str(error) if isinstance(error,EvidenceError) else 'AMZN_OPERATING_BASIS_CHANGED','AMZN_OPERATING_BASIS')
    # The preceding release establishes the non-operating gain explicitly. It
    # remains original evidence, never inferred from GAAP EPS differences.
    candidates=[i for i,form in enumerate(recent.get('form',[])) if form=='8-K'
        and '2.02' in recent.get('items',['']*len(recent['form']))[i]
        and parse_time(recent['acceptanceDateTime'][i])<=parse_time(now)]
    if len(candidates)<2:return
    i=candidates[1]
    accession=recent['accessionNumber'][i].replace('-','');document=recent['primaryDocument'][i]
    url=f'https://www.sec.gov/Archives/edgar/data/1018724/{accession}/{document}'
    try:
        filing=_fetch(store,url,now,transport);parser=_HTML()
        parser.feed(base64.b64decode(filing['raw_base64']).decode('utf-8-sig'))
        link=next(urljoin(url,href) for href,label in parser.links if re.search(r'99[._-]?1|99\.1',href+' '+label,re.I) and _allowed(urljoin(url,href)))
        release=_fetch(store,link,now,transport)
        ev=_evidence(store,release,published_at=recent['acceptanceDateTime'][i]);out['primary_evidence'].append(ev)
        out['primary_financials']['previous_earnings_release']=dict(evidence_refs=[ev['id']],source_url=link,
            published_at=recent['acceptanceDateTime'][i],instruction='Original preceding-quarter release: separate investment gains from recurring operating income.')
    except Exception:
        _gap(out['primary_gaps'],'AMZN_PREVIOUS_RELEASE_UNAVAILABLE','AMZN_NONOPERATING_GAIN',url)


def enrich_primary_fundamentals(store, symbol, fundamentals, *, now=None, transport=None):
    """Return public facts separately from native provider data and forecasts.

    Explicit issuer adapters are used. ``now`` is the information cutoff;
    fetching a newer source never changes its source date to that cutoff.
    """
    symbol = symbol_value(symbol)
    now = parse_time(now or utc_now()).isoformat()
    out = copy.deepcopy(fundamentals or {})
    out["symbol"] = symbol
    if symbol not in {"TSM", "GOOG", "GOOGL", "CRCL", "SOXX", "QQQ", *SEC_ISSUERS}:
        out['primary_gaps']=[{'code':'PRIMARY_ISSUER_ADAPTER_UNAVAILABLE','field':symbol,
            'source':'official-primary','blocking':True}]
        out['primary_refresh_after']=(parse_time(now)+timedelta(hours=6)).isoformat()
        return out
    out.update(primary_financials={"symbol": symbol, "facts": [], "instrument": {}}, primary_evidence=[], primary_gaps=[])
    out.pop("etf_valuation", None)
    if transport is None:
        import requests
        transport = requests.Session()
    try:
        collector = _sec_equity if symbol in SEC_ISSUERS else {"TSM": _tsm, "GOOG": _google, "GOOGL": _google, "CRCL": _circle, "SOXX": _soxx, "QQQ": _qqq}[symbol]
        collector(store, out, now, transport)
    except Exception as exc:
        _gap(out["primary_gaps"], str(exc) if isinstance(exc, EvidenceError) else "PRIMARY_SOURCE_UNAVAILABLE", symbol)
    out["primary_evidence"] = list({item["id"]: item for item in out["primary_evidence"]}.values())
    # No observation timestamp in this semantic digest: identical facts cannot
    # queue a fresh model review on every fetch or service restart.
    out["primary_evidence_hash"] = stable_id(VERSION, symbol,
        [(item["id"], item["sha256"]) for item in out["primary_evidence"]],
        out["primary_financials"], out.get("etf_valuation"))
    retry = (any(gap.get("blocking") is True for gap in out["primary_gaps"])
             or (out.get("primary_collection") or {}).get("retry_required") is True)
    out["primary_refresh_after"] = (parse_time(now) + timedelta(hours=6 if retry else 24)).isoformat()
    return out
