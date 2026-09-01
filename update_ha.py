#!/usr/bin/env python3
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from dateutil import parser as dtparser
import requests, json, re, unicodedata, time

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
ENTRY_URL = "https://www.ha.org.hk/visitor/ha_browse_act.asp?Content_ID=2001&Lang=ENG"
HEADERS = {"User-Agent":"Mozilla/5.0 (compatible; HA-Tender-Research/1.0)"}
TIMEOUT = 35

def now_iso(): return datetime.now().astimezone().isoformat(timespec="seconds")
def clean(s): return re.sub(r"\s+"," ",unicodedata.normalize("NFKC",str(s or ""))).strip()
def keytext(s): return clean(s).upper()

def get(url, session):
    last=None
    for i in range(3):
        try:
            r=session.get(url,headers=HEADERS,timeout=TIMEOUT)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower()=="iso-8859-1":
                r.encoding=r.apparent_encoding or "utf-8"
            return r
        except Exception as e:
            last=e
            time.sleep(1.5*(i+1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")

def parse_date(s):
    s=clean(s)
    if not s:return ""
    s=re.sub(r"\bat\b.*$","",s,flags=re.I).strip()
    for dayfirst in (True,False):
        try:
            d=dtparser.parse(s,dayfirst=dayfirst,fuzzy=True)
            if 1990<=d.year<=2100:return d.date().isoformat()
        except Exception:
            pass
    return ""

def canon_text(s):
    s=unicodedata.normalize("NFKC",str(s or "")).upper()
    s=s.replace("–","-").replace("—","-").replace("−","-").replace("�"," ")
    s=re.sub(r"[^A-Z0-9]+"," ",s)
    return re.sub(r"\s+"," ",s).strip()

def canonical_contractor(s):
    s=clean(s)
    m=re.match(
        r"^(.*?\b(?:LIMITED|LTD\.?|CO\.,?\s*LTD\.?|INC\.?|INCORPORATED|CORPORATION|CORP\.?|COMPANY LIMITED|COMPANY LTD\.?))\b",
        s,re.I
    )
    if m:return keytext(m.group(1))
    return keytext(re.split(
        r"\b(?:UNIT|ROOMS?|RM\.?|FLAT|G/F|LG/F|UG/F|[0-9]+/F|LEVEL|SUITE|NO\.|BLOCK|TOWER|AREA)\b",
        s,maxsplit=1,flags=re.I
    )[0])

def canon_item(s):
    t=canon_text(s)
    return "-" if t in {"","N A","NA"} else t

def canon_amount(s):
    s=clean(s).upper()
    if not s or s in {"-","--","N/A","NA"}:return "-"
    cur=""
    if "HKD" in s or "HK$" in s or s.startswith("$"):cur="HKD"
    elif "RMB" in s or "CNY" in s:cur="RMB"
    elif "USD" in s or "US$" in s:cur="USD"
    elif "EUR" in s:cur="EUR"
    m=re.search(r"([-+]?\d[\d,]*(?:\.\d+)?)",s)
    if not m:return canon_text(s)
    val=Decimal(m.group(1).replace(",",""))
    if "MN" in s or "MILLION" in s:val*=Decimal(1000000)
    elif "THOUSAND" in s or re.search(r"(?<![A-Z])K(?![A-Z])",s):val*=Decimal(1000)
    sval=format(val,"f")
    if "." in sval:sval=sval.rstrip("0").rstrip(".")
    return f"{cur}:{sval}" if cur else sval

def canon_period(r):
    cs=clean(r.get("Contract Start"))
    ce=clean(r.get("Contract End"))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}",cs or "") and re.fullmatch(r"\d{4}-\d{2}-\d{2}",ce or ""):
        return f"{cs}:{ce}"
    hits=re.findall(r"\b\d{1,2}\s+[A-Za-z]+\s+\d{4}\b",clean(r.get("Contract Period")))
    if len(hits)>=2:
        a=parse_date(hits[0]);b=parse_date(hits[1])
        if a and b:return f"{a}:{b}"
    hits2=re.findall(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b",clean(r.get("Contract Period")))
    if len(hits2)>=2:
        a=parse_date(hits2[0]);b=parse_date(hits2[1])
        if a and b:return f"{a}:{b}"
    return canon_text(r.get("Contract Period"))

def award_base_key(r):
    return "|".join([
        keytext(r.get("Tender Reference")),
        clean(r.get("Award Date")),
        canonical_contractor(r.get("Contractor")),
        canon_item(r.get("Item")),
    ])

def award_key(r):
    # V2: preserve legitimate multi-lines under the same tender/date/contractor/item.
    # Amount formatting is canonicalized, e.g. HKD0.36Mn == HKD360,000.
    return "|".join([
        award_base_key(r),
        canon_amount(r.get("Estimated Contract Amount Raw")),
        canon_period(r),
    ])

def notice_key(r):
    return keytext(r.get("Tender Reference")) or "|".join([keytext(r.get("Subject")),clean(r.get("Issue Date"))])

def product_object(subject):
    s=clean(subject)
    s=re.sub(r"^(Tender for the |Supply and Installation of |Supply of |Provision of )","",s,flags=re.I)
    return clean(s)

def contract_dates(period):
    hits=re.findall(r"\b\d{1,2}\s+[A-Za-z]+\s+\d{4}\b",clean(period))
    if len(hits)>=2:return parse_date(hits[0]),parse_date(hits[1])
    return "",""

def discover_article(session,label):
    r=get(ENTRY_URL,session)
    soup=BeautifulSoup(r.text,"lxml")
    for a in soup.find_all("a",href=True):
        if label.upper() in clean(a.get_text(" ",strip=True)).upper():
            return urljoin(ENTRY_URL,a["href"])
    raise RuntimeError(f"Could not find {label} on HA Tender Notices entry page")

def candidate_links(base,html):
    """Extract HA candidate URLs from normal tags and embedded JavaScript/raw HTML."""
    soup=BeautifulSoup(html,"lxml")
    out=set()

    for tag in soup.find_all(["a","iframe","frame","embed","object","form"]):
        for attr in ("href","src","data","action"):
            v=tag.get(attr)
            if v:
                out.add(urljoin(base,v.strip()))

    for v in re.findall(r"(?:href|src|data|action)\s*=\s*[\"']([^\"']+)[\"']",html,re.I):
        out.add(urljoin(base,v.strip()))

    for v in re.findall(r"[\"']([^\"'<>\s]+\.html?(?:\?[^\"']*)?)[\"']",html,re.I):
        out.add(urljoin(base,v.strip()))

    for v in re.findall(r"(?i)(?:https?://[^\s\"'<>]+)?/?(?:haho/ho/bssd/)?TA_[A-Za-z0-9_-]+\.html?",html):
        out.add(urljoin(base,v.strip()))

    for v in re.findall(r"(?i)(/?haho/ho/bssd/[^\s\"'<>]+\.html?)",html):
        out.add(urljoin(base,v.strip()))

    return sorted(u for u in out if urlparse(u).netloc.lower().endswith("ha.org.hk"))


def resolve_contract_page(session,wrapper):
    wr=get(wrapper,session)
    text=clean(BeautifulSoup(wr.text,"lxml").get_text(" ",strip=True)).lower()
    if "contract award notice" in text and "date of award" in text:
        return wrapper,wr.text

    links=candidate_links(wrapper,wr.text)
    links=sorted(links,key=lambda u:("TA_" not in u.upper(),"/haho/ho/bssd/" not in u.lower(),u))

    attempted=[]
    for u in links:
        if not re.search(r"\.html?(?:$|\?)",u,re.I):
            continue
        try:
            attempted.append(u)
            rr=get(u,session)
            t=clean(BeautifulSoup(rr.text,"lxml").get_text(" ",strip=True)).lower()
            if "contract award notice" in t and "tender reference" in t and "date of award" in t:
                return u,rr.text
        except Exception:
            pass

    # Emergency fallback for the currently published HA Contract Award page.
    # Automatic discovery above is still the primary path; this only prevents a
    # wrapper-markup change from breaking the daily database immediately.
    fallback_urls=[
        "https://www.ha.org.hk/haho/ho/bssd/TA_236491_210026500a.htm"
    ]
    for u in fallback_urls:
        if u in attempted:
            continue
        try:
            attempted.append(u)
            rr=get(u,session)
            t=clean(BeautifulSoup(rr.text,"lxml").get_text(" ",strip=True)).lower()
            if "contract award notice" in t and "tender reference" in t and "date of award" in t:
                print("Using emergency Contract Award fallback:",u)
                return u,rr.text
        except Exception:
            pass

    print("Contract Award wrapper URL:",wrapper)
    print("Candidate HTML links discovered:",len(links))
    for u in links[:30]:
        print("  candidate:",u)
    if attempted:
        print("Attempted contract page URLs:")
        for u in attempted[:30]:
            print("  attempted:",u)

    raise RuntimeError(
        "Could not resolve current HA Contract Award detail page. "
        "See candidate URLs printed above in the GitHub Actions log."
    )


def parse_awards(html,source_url):
    soup=BeautifulSoup(html,"lxml")
    records=[]
    current_month=""
    for tr in soup.find_all("tr"):
        cells=[clean(x.get_text(" ",strip=True)) for x in tr.find_all(["th","td"],recursive=False)]
        if not cells:
            cells=[clean(x.get_text(" ",strip=True)) for x in tr.find_all(["th","td"])]
        if not cells:continue
        rowtxt=" ".join(cells)
        mm=re.search(r"Awarded Month\s*:\s*([A-Za-z]+\s+\d{4})",rowtxt,re.I)
        if mm:
            current_month=clean(mm.group(1))
            continue
        if len(cells)<9:continue
        if keytext(cells[0])=="HOSPITAL" or "TENDER REFERENCE" in keytext(cells[1]):continue
        hospital,tref,subject,procedure,contractor,item,period,amount,award_raw=cells[:9]
        award_date=parse_date(award_raw)
        if not tref or not subject or not award_date:continue
        cs,ce=contract_dates(period)
        r={"Award Month":award_date[:7]+"-01","Award Date":award_date,"Source Section Month":current_month,
           "Tender Reference":tref,"Hospital / Cluster":hospital,"Product / Tender Object":product_object(subject),
           "Subject":subject,"Tendering Procedure":procedure,"Contractor":contractor,"Item":item,
           "Contract Start":cs,"Contract End":ce,"Contract Period":period,"Estimated Contract Amount Raw":amount,
           "Capture Date":date.today().isoformat(),"Source File":"","Date of Award Raw":award_raw,"Source URL":source_url}
        r["Unique Key"]=award_key(r)
        r["_ContractorShort"]=canonical_contractor(contractor).title()
        r["_search"]=" ".join(clean(v) for v in r.values()).lower()
        records.append(r)
    if not records:
        raise RuntimeError("HA Contract Award page returned zero parseable rows; existing database kept")
    return list({r["Unique Key"]:r for r in records}.values())

def parse_notice_page(html,url):
    soup=BeautifulSoup(html,"lxml")
    visible=" ".join(soup.stripped_strings)
    if "Tender Reference" not in visible or "Closing Date" not in visible:return None
    fields={"Tender Reference":"","Subject":"","Issue Date":"","Closing Date Raw":"","Submission Requirement":"","Tender Enquiry":""}
    label_map={"TENDER REFERENCE":"Tender Reference","SUBJECT MATTER":"Subject","ISSUE DATE":"Issue Date",
               "CLOSING DATE AND TIME":"Closing Date Raw","CLOSING DATE":"Closing Date Raw",
               "SUBMISSION REQUIREMENT":"Submission Requirement","TENDER ENQUIRY":"Tender Enquiry"}
    for tr in soup.find_all("tr"):
        cells=[clean(x.get_text(" ",strip=True)) for x in tr.find_all(["th","td"])]
        cells=[x for x in cells if x and x!=":"]
        if len(cells)>=2:
            lab=keytext(cells[0].rstrip(":"))
            if lab in label_map:fields[label_map[lab]]=clean(" ".join(cells[1:]))
    patterns={
      "Tender Reference":r"Tender Reference\s*:?\s*(.+?)(?=Subject Matter|Issue Date|Closing Date)",
      "Subject":r"Subject Matter\s*:?\s*(.+?)(?=Issue Date|Closing Date)",
      "Issue Date":r"Issue Date\s*:?\s*(.+?)(?=Closing Date)",
      "Closing Date Raw":r"Closing Date(?: and Time)?\s*:?\s*(.+?)(?=Submission Requirement|Tender Enquiry|Tender Documents|Links for)"
    }
    for f,p in patterns.items():
        if not fields[f]:
            m=re.search(p,visible,re.I)
            if m:fields[f]=clean(m.group(1))
    if not fields["Tender Reference"] or not fields["Subject"]:return None
    fields["Issue Date"]=parse_date(fields["Issue Date"])
    fields["Closing Date"]=parse_date(fields["Closing Date Raw"])
    fields["Source URL"]=url
    fields["Unique Key"]=notice_key(fields)
    return fields

def notice_crawl_candidate(url):
    """Keep the notice crawl narrowly inside HA procurement/index HTML pages."""
    p=urlparse(url)
    if not p.netloc.lower().endswith("ha.org.hk"):
        return False

    path=p.path.lower()
    # Skip documents/assets; the frontend needs notice metadata, not tender documents.
    if re.search(r"\.(?:pdf|docx?|xlsx?|xls|zip|jpg|jpeg|png|gif|css|js)(?:$|\?)",url,re.I):
        return False

    # Procurement static pages and HA content wrappers/indexes are the useful paths.
    if "/haho/ho/bssd/" in path:
        return True
    if "/visitor/ha_view_content.asp" in path:
        return True

    return False


def collect_notices(session,wrapper):
    """
    Crawl the HA Tender Notices section through intermediate wrapper/index pages.

    The HA site does not always link the individual /haho/ho/bssd/*.htm notices
    directly from the first wrapper page. A shallow BFS is therefore used:
      Tender Notices wrapper
        -> intermediate HA content/index page(s)
        -> individual tender notice detail pages

    A page is stored only if parse_notice_page() finds the required notice fields.
    """
    records=[]
    visited=set()
    queue=[(wrapper,0)]
    max_depth=3
    max_pages=350

    while queue and len(visited)<max_pages:
        url,depth=queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        try:
            rr=get(url,session)
        except Exception as e:
            print("Notice crawl fetch failed:",url,str(e))
            continue

        rec=parse_notice_page(rr.text,url)
        if rec:
            records.append(rec)
            # Detail pages mainly link to tender documents; no need to fan out further.
            continue

        if depth>=max_depth:
            continue

        links=candidate_links(url,rr.text)

        # Also catch unquoted HA procurement HTML paths embedded in legacy JavaScript.
        for v in re.findall(r"(?i)(/?haho/ho/bssd/[A-Za-z0-9_().%+\-/]+\.html?(?:\?[^\\s\"'<>]*)?)",rr.text):
            links.append(urljoin(url,v.strip()))

        for nxt in sorted(set(links)):
            if nxt not in visited and notice_crawl_candidate(nxt):
                queue.append((nxt,depth+1))

    unique={r["Unique Key"]:r for r in records}

    print("Tender Notice wrapper URL:",wrapper)
    print("Tender Notice pages crawled:",len(visited))
    print("Tender Notices parsed:",len(unique))

    if not unique:
        print("WARNING: No Tender Notices parsed. Existing notices.json will be preserved.")

    return list(unique.values())

def load_json(name,default):
    try:return json.loads((DATA_DIR/name).read_text(encoding="utf-8"))
    except Exception:return default

def save_json(name,obj,pretty=False):
    p=DATA_DIR/name
    tmp=p.with_suffix(p.suffix+".tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2 if pretty else None,separators=None if pretty else (",",":")),encoding="utf-8")
    tmp.replace(p)

def save_current_snapshot(incoming_awards):
    """Current HA page only. Re-key/deduplicate under V2 before writing."""
    by={}
    for raw in incoming_awards:
        r=dict(raw)
        k=award_key(r)
        r["Unique Key"]=k
        r["Record Key"]=k
        by[k]=r
    save_json("current_awards.json", list(by.values()))

def validate_master_never_shrinks(before, after):
    """
    Historical master must never lose unique lines just because HA's current webpage
    stops displaying an older month. If this invariant is violated, abort before write.
    """
    before_keys={award_key(r) for r in before}
    after_keys={award_key(r) for r in after}
    missing=before_keys-after_keys
    if missing:
        sample=sorted(missing)[:5]
        raise RuntimeError(
            f"Safety stop: historical master would lose {len(missing)} existing unique lines. "
            f"Nothing was overwritten. Sample missing keys: {sample}"
        )

def build_historical_base(seed_rows, existing_rows):
    """
    Self-healing V2 historical base. Every row is re-keyed using the current
    V2 logic so old V1 keys can never reintroduce collapsed/duplicate records.
    """
    by={}
    for source in (seed_rows, existing_rows):
        for raw in source:
            r=dict(raw)
            k=award_key(r)
            r["Unique Key"]=k
            r["Record Key"]=k
            by[k]=r
    return list(by.values())

def validate_seed_preserved(seed_rows, master_rows):
    seed_keys={award_key(r) for r in seed_rows}
    master_keys={award_key(r) for r in master_rows}
    missing=seed_keys-master_keys
    if missing:
        sample=sorted(missing)[:5]
        raise RuntimeError(
            f"Safety stop: master is missing {len(missing)} immutable historical seed lines. "
            f"Nothing was overwritten. Sample missing keys: {sample}"
        )

COMPARE_AWARD=["Hospital / Cluster","Subject","Tendering Procedure","Contractor","Item","Contract Period","Estimated Contract Amount Raw","Award Date"]
COMPARE_NOTICE=["Subject","Issue Date","Closing Date","Submission Requirement","Tender Enquiry"]

def merge_awards(existing,incoming):
    # Re-key everything under V2.
    by={}
    for raw in existing:
        r=dict(raw)
        k=award_key(r)
        r["Unique Key"]=k
        r["Record Key"]=k
        by[k]=r

    incoming_groups={}
    for raw in incoming:
        r=dict(raw)
        k=award_key(r)
        r["Unique Key"]=k
        r["Record Key"]=k
        incoming_groups.setdefault(award_base_key(r),[]).append(r)

    existing_groups={}
    for k,r in by.items():
        existing_groups.setdefault(award_base_key(r),[]).append(k)

    new=updated=0
    today=date.today().isoformat()

    for base, inc_rows in incoming_groups.items():
        existing_keys=list(existing_groups.get(base,[]))

        for r in inc_rows:
            k=r["Unique Key"]
            if k in by:
                old=by[k]
                old["Last Seen"]=today
                old["Times Observed"]=int(old.get("Times Observed") or 1)+1
                changed=[f for f in COMPARE_AWARD if clean(old.get(f))!=clean(r.get(f))]
                if changed:
                    for f in changed:old[f]=r.get(f,"")
                    old["Version Change Flag"]="YES"
                    old["Source URL"]=r.get("Source URL","")
                    updated+=1
                continue

            # If both source and master contain exactly one logical line for this
            # base identity, treat an amount/period change as a version update
            # instead of manufacturing a duplicate.
            if len(inc_rows)==1 and len(existing_keys)==1:
                old_key=existing_keys[0]
                old=by.pop(old_key)
                changed=[f for f in COMPARE_AWARD if clean(old.get(f))!=clean(r.get(f))]
                for f in changed:old[f]=r.get(f,"")
                old["Last Seen"]=today
                old["Times Observed"]=int(old.get("Times Observed") or 1)+1
                old["Version Change Flag"]="YES"
                old["Source URL"]=r.get("Source URL","")
                new_key=award_key(old)
                old["Unique Key"]=new_key
                old["Record Key"]=new_key
                by[new_key]=old
                existing_groups[base]=[new_key]
                updated+=1
            else:
                r["First Seen"]=today
                r["Last Seen"]=today
                r["Times Observed"]=1
                r["Version Change Flag"]=""
                by[k]=r
                existing_groups.setdefault(base,[]).append(k)
                new+=1

    return sorted(by.values(),key=lambda r:r.get("Award Date",""),reverse=True),new,updated

def merge_notices(existing,incoming):
    by={}
    for r in existing:
        k=r.get("Unique Key") or notice_key(r)
        r["Unique Key"]=k
        by[k]=r
    new=updated=0
    today=date.today().isoformat()
    for r in incoming:
        k=r["Unique Key"]
        if k not in by:
            r["First Seen"]=today;r["Last Seen"]=today;r["Times Observed"]=1;r["Version Change Flag"]=""
            by[k]=r;new+=1
        else:
            old=by[k]
            old["Last Seen"]=today
            old["Times Observed"]=int(old.get("Times Observed") or 1)+1
            changed=[f for f in COMPARE_NOTICE if clean(old.get(f))!=clean(r.get(f))]
            if changed:
                for f in changed:old[f]=r.get(f,"")
                old["Version Change Flag"]="YES"
                old["Source URL"]=r.get("Source URL","")
                updated+=1
    for r in by.values():
        cd=r.get("Closing Date","")
        r["Status"]="OPEN" if cd and cd>=today else ("CLOSED" if cd else "UNKNOWN")
    return sorted(by.values(),key=lambda r:r.get("Closing Date",""),reverse=True),new,updated

def main():
    DATA_DIR.mkdir(exist_ok=True)
    meta=load_json("meta.json",{})
    meta["last_checked"]=now_iso()
    session=requests.Session()
    try:
        award_wrapper=discover_article(session,"CONTRACT AWARD NOTICE")
        award_url,award_html=resolve_contract_page(session,award_wrapper)
        incoming_awards=parse_awards(award_html,award_url)

        # Keep today's HA webpage snapshot separate from the permanent historical master.
        save_current_snapshot(incoming_awards)

        # Immutable historical seed makes the master self-healing.
        # Even if awards.json is accidentally reduced to the current HA snapshot,
        # the next run reconstructs the full historical base before adding today's data.
        historical_seed=load_json("historical_seed.json",[])
        existing_awards=load_json("awards.json",[])
        historical_base=build_historical_base(historical_seed,existing_awards)

        awards,a_new,a_upd=merge_awards(historical_base,incoming_awards)
        validate_seed_preserved(historical_seed,awards)
        validate_master_never_shrinks(historical_base,awards)

        notice_wrapper=discover_article(session,"TENDER NOTICES")
        incoming_notices=collect_notices(session,notice_wrapper)
        existing_notices=load_json("notices.json",[])
        if incoming_notices:
            notices,n_new,n_upd=merge_notices(existing_notices,incoming_notices)
        else:
            notices=existing_notices
            n_new=n_upd=0

        save_json("awards.json",awards)
        save_json("notices.json",notices)

        award_dates=[r.get("Award Date","") for r in awards if r.get("Award Date")]
        if award_dates:
            print(f"Historical master range: {min(award_dates)} -> {max(award_dates)}")
        print(f"Immutable historical seed rows: {len(historical_seed)}")
        print(f"Existing master before repair: {len(existing_awards)}")
        print(f"Historical base after seed repair: {len(historical_base)}")
        print(f"Current HA snapshot rows: {len(incoming_awards)}")
        print(f"Permanent historical master rows: {len(awards)}")
        meta.update({"last_successful_update":now_iso(),"award_count":len(awards),"notice_count":len(notices),
                     "new_awards_last_run":a_new,"updated_awards_last_run":a_upd,
                     "new_notices_last_run":n_new,"updated_notices_last_run":n_upd,
                     "award_source_url":award_url,"error":""})
        print(f"Awards: {len(awards)} total, +{a_new} new, {a_upd} updated")
        print(f"Notices: {len(notices)} total, +{n_new} new, {n_upd} updated")
    except Exception as e:
        meta["error"]=str(e)
        save_json("meta.json",meta,True)
        raise
    save_json("meta.json",meta,True)

if __name__=="__main__":
    main()
