#!/usr/bin/env python3
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime, date
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

def canonical_contractor(s):
    s=clean(s)
    m=re.match(r"^(.*?\b(?:LIMITED|LTD\.?|CO\.,?\s*LTD\.?|INC\.?|CORPORATION|CORP\.?))\b",s,re.I)
    if m:return keytext(m.group(1))
    return keytext(re.split(r"\b(?:UNIT|ROOM|RM\.?|FLAT|G/F|[0-9]+/F|LEVEL|SUITE|NO\.|BLOCK|TOWER)\b",s,maxsplit=1,flags=re.I)[0])

def award_key(r):
    item=keytext(r.get("Item"))
    if item in {"","-","--","N/A","NA","�@"}:
        item=keytext(r.get("Product / Tender Object") or r.get("Subject"))
    return "|".join([keytext(r.get("Tender Reference")),clean(r.get("Award Date")),canonical_contractor(r.get("Contractor")),item])

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
    soup=BeautifulSoup(html,"lxml")
    out=set()
    for tag in soup.find_all(["a","iframe","frame"]):
        v=tag.get("href") or tag.get("src")
        if v:out.add(urljoin(base,v))
    for v in re.findall(r"(?:href|src)\s*=\s*[\"']([^\"']+)[\"']",html,re.I):
        out.add(urljoin(base,v))
    return [u for u in out if urlparse(u).netloc.lower().endswith("ha.org.hk")]

def resolve_contract_page(session,wrapper):
    wr=get(wrapper,session)
    text=clean(BeautifulSoup(wr.text,"lxml").get_text(" ",strip=True)).lower()
    if "contract award notice" in text and "date of award" in text:return wrapper,wr.text
    links=sorted(candidate_links(wrapper,wr.text),key=lambda u:("/haho/ho/bssd/" not in u.lower(),u))
    for u in links:
        if not re.search(r"\.html?(?:$|\?)",u,re.I):continue
        try:
            rr=get(u,session)
            t=clean(BeautifulSoup(rr.text,"lxml").get_text(" ",strip=True)).lower()
            if "contract award notice" in t and "tender reference" in t and "date of award" in t:
                return u,rr.text
        except Exception:
            pass
    raise RuntimeError("Could not resolve current HA Contract Award detail page")

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

def collect_notices(session,wrapper):
    wr=get(wrapper,session)
    records=[]
    direct=parse_notice_page(wr.text,wrapper)
    if direct:records.append(direct)
    for u in sorted(set(candidate_links(wrapper,wr.text))):
        if "/haho/ho/bssd/" not in u.lower() or not re.search(r"\.html?(?:$|\?)",u,re.I):continue
        try:
            rr=get(u,session)
            rec=parse_notice_page(rr.text,u)
            if rec:records.append(rec)
        except Exception:
            pass
    return list({r["Unique Key"]:r for r in records}.values())

def load_json(name,default):
    try:return json.loads((DATA_DIR/name).read_text(encoding="utf-8"))
    except Exception:return default

def save_json(name,obj,pretty=False):
    p=DATA_DIR/name
    tmp=p.with_suffix(p.suffix+".tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2 if pretty else None,separators=None if pretty else (",",":")),encoding="utf-8")
    tmp.replace(p)

COMPARE_AWARD=["Hospital / Cluster","Subject","Tendering Procedure","Contractor","Item","Contract Period","Estimated Contract Amount Raw","Award Date"]
COMPARE_NOTICE=["Subject","Issue Date","Closing Date","Submission Requirement","Tender Enquiry"]

def merge_awards(existing,incoming):
    by={}
    for r in existing:
        k=r.get("Unique Key") or award_key(r)
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
            changed=[f for f in COMPARE_AWARD if clean(old.get(f))!=clean(r.get(f))]
            if changed:
                for f in changed:old[f]=r.get(f,"")
                old["Version Change Flag"]="YES"
                old["Source URL"]=r.get("Source URL","")
                updated+=1
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
        awards,a_new,a_upd=merge_awards(load_json("awards.json",[]),incoming_awards)

        notice_wrapper=discover_article(session,"TENDER NOTICES")
        incoming_notices=collect_notices(session,notice_wrapper)
        notices,n_new,n_upd=merge_notices(load_json("notices.json",[]),incoming_notices)

        save_json("awards.json",awards)
        save_json("notices.json",notices)
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
