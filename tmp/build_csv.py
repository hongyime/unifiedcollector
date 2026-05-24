#!/usr/bin/env python3
"""Build sg_schools.csv from data.gov.sg MOE schools dataset + manual unis/polys/intl."""
import json, csv, re, os

SRC = r'C:\unifiedcollector\tmp\datagov.json'
OUT = r'C:\unifiedcollector\config\sg_schools.csv'

def norm_url(u: str) -> str:
    u = u.strip()
    if not u:
        return ''
    if not re.match(r'^https?://', u, re.I):
        u = 'https://' + u
    # force https
    u = re.sub(r'^http://', 'https://', u, flags=re.I)
    # ensure trailing slash for bare hostnames
    if re.match(r'^https://[^/]+$', u):
        u += '/'
    return u

KNOWN_ACRONYMS = {
    'CHIJ','SJI','ACS','MGS','PLMGS','SCGS','RGS','RGPS','RI','HCI','NJC','VJC','TJC',
    'NYJC','SAJC','PJC','RJC','EJC','JJC','MJC','TPJC','YIJC','MI','NUS','NTU','SMU',
    'SUTD','SIT','SUSS','NAFA','NPS','GIIS','SAS','SOTA','APSN','MINDS','AWWA','UWC',
    'UWCSEA','ITE','ESSEC','INSEAD','MDIS','EASB','PSB','XCL','OWIS','IB','DPS','NPSI',
    'CIS','GESS','LFS','HCIS','ISS','OFS','SBC','TTC','XWA','IIS','ASRJC','TMJC','JPJC'
}

def title_case(s: str) -> str:
    s = s.strip()
    parts = s.split()
    smalls = {'of','the','and','for','in','at','on','to','a','an'}
    out = []
    for i, p in enumerate(parts):
        m = re.match(r'^([(\[]*)([\w\'-]+)([)\]]*[.,]?)$', p)
        if m:
            pre, core, post = m.groups()
            cl = core.lower()
            if i not in (0, len(parts)-1) and cl in smalls:
                out.append(pre + cl + post)
            elif core.upper() in KNOWN_ACRONYMS:
                out.append(pre + core.upper() + post)
            else:
                # Title-case: handle hyphens/apostrophes
                tokens = re.split(r"([-'])", core)
                tc_parts = []
                for i_t, t in enumerate(tokens):
                    if i_t % 2 == 1:  # delimiter
                        tc_parts.append(t)
                    else:
                        # Don't capitalize a single 's' or 't' after an apostrophe (e.g. Anthony's, don't)
                        if i_t > 0 and tokens[i_t-1] == "'" and len(t) <= 2:
                            tc_parts.append(t.lower())
                        else:
                            tc_parts.append(t.capitalize())
                tc = ''.join(tc_parts)
                out.append(pre + tc + post)
        else:
            out.append(p.capitalize())
    return ' '.join(out)

with open(SRC, encoding='utf-8') as f:
    recs = json.load(f)['result']['records']

rows = []  # (name, url, category, notes)

def cat_for(rec) -> tuple:
    lvl = rec['mainlevel_code']
    notes_bits = []
    if rec.get('sap_ind') == 'Yes': notes_bits.append('SAP')
    if rec.get('autonomous_ind') == 'Yes': notes_bits.append('Autonomous')
    if rec.get('gifted_ind') == 'Yes': notes_bits.append('GEP')
    if rec.get('ip_ind') == 'Yes': notes_bits.append('IP')
    notes_bits.append(rec.get('type_code',''))
    notes = '; '.join(b for b in notes_bits if b)
    if lvl == 'PRIMARY':
        return ('primary', notes)
    if lvl.startswith('SECONDARY'):
        return ('secondary', notes)
    if lvl == 'JUNIOR COLLEGE' or lvl == 'CENTRALISED INSTITUTE':
        return ('jc', notes)
    if lvl.startswith('MIXED LEVEL'):
        # treat S1-JC2 / S1-S5,JC1-JC2 as secondary (IP through-train); P1-S4 as primary
        if 'P1' in lvl:
            return ('primary', notes + '; through-train P1-S4')
        return ('secondary', notes + '; IP/through-train')
    return ('other', notes)

seen_urls = set()
for r in recs:
    name = title_case(r['school_name'])
    url = norm_url(r['url_address'])
    if not url or url in seen_urls:
        continue
    seen_urls.add(url)
    cat, notes = cat_for(r)
    rows.append((name, url, cat, notes))

# --- Manually curated tertiary / specialized / international additions ---
# All URLs are well-known official domains.
extra = [
    # Autonomous Universities
    ("National University of Singapore", "https://www.nus.edu.sg/", "uni", "Autonomous university"),
    ("Nanyang Technological University", "https://www.ntu.edu.sg/", "uni", "Autonomous university"),
    ("Singapore Management University", "https://www.smu.edu.sg/", "uni", "Autonomous university"),
    ("Singapore University of Technology and Design", "https://www.sutd.edu.sg/", "uni", "Autonomous university"),
    ("Singapore Institute of Technology", "https://www.singaporetech.edu.sg/", "uni", "Autonomous university"),
    ("Singapore University of Social Sciences", "https://www.suss.edu.sg/", "uni", "Autonomous university"),

    # Private universities / foreign branch campuses
    ("SIM Global Education", "https://www.sim.edu.sg/", "uni", "Private; SIM"),
    ("Singapore Institute of Management", "https://www.sim.edu.sg/", "uni", "Private"),
    ("James Cook University Singapore", "https://www.jcu.edu.sg/", "uni", "Foreign branch campus"),
    ("Curtin Singapore", "https://www.curtin.edu.sg/", "uni", "Foreign branch campus"),
    ("INSEAD Asia Campus", "https://www.insead.edu/", "uni", "Foreign branch campus"),
    ("ESSEC Asia-Pacific", "https://www.essec.edu/en/asia-pacific/", "uni", "Foreign branch campus"),
    ("Digipen Institute of Technology Singapore", "https://www.digipen.edu.sg/", "uni", "Private"),
    ("PSB Academy", "https://www.psb-academy.edu.sg/", "uni", "Private"),
    ("Kaplan Higher Education Singapore", "https://www.kaplan.com.sg/", "uni", "Private"),
    ("MDIS - Management Development Institute of Singapore", "https://www.mdis.edu.sg/", "uni", "Private"),
    ("EASB East Asia Institute of Management", "https://www.easb.edu.sg/", "uni", "Private"),
    ("Amity Global Institute", "https://www.amity.edu.sg/", "uni", "Private"),

    # Polytechnics
    ("Singapore Polytechnic", "https://www.sp.edu.sg/", "poly", "Polytechnic"),
    ("Ngee Ann Polytechnic", "https://www.np.edu.sg/", "poly", "Polytechnic"),
    ("Temasek Polytechnic", "https://www.tp.edu.sg/", "poly", "Polytechnic"),
    ("Nanyang Polytechnic", "https://www.nyp.edu.sg/", "poly", "Polytechnic"),
    ("Republic Polytechnic", "https://www.rp.edu.sg/", "poly", "Polytechnic"),

    # ITE
    ("Institute of Technical Education", "https://www.ite.edu.sg/", "poly", "ITE HQ"),
    ("ITE College Central", "https://www.ite.edu.sg/colleges/ite-college-central", "poly", "ITE college"),
    ("ITE College East", "https://www.ite.edu.sg/colleges/ite-college-east", "poly", "ITE college"),
    ("ITE College West", "https://www.ite.edu.sg/colleges/ite-college-west", "poly", "ITE college"),

    # Arts institutions
    ("Nanyang Academy of Fine Arts", "https://www.nafa.edu.sg/", "other", "Arts institution; NAFA"),
    ("LASALLE College of the Arts", "https://www.lasalle.edu.sg/", "other", "Arts institution"),
    ("University of the Arts Singapore", "https://www.uas.edu.sg/", "uni", "NAFA + LASALLE alliance"),

    # International schools
    ("Singapore American School", "https://www.sas.edu.sg/", "intl", "International school"),
    ("Tanglin Trust School", "https://www.tts.edu.sg/", "intl", "International school"),
    ("United World College of South East Asia", "https://www.uwcsea.edu.sg/", "intl", "UWCSEA"),
    ("Australian International School Singapore", "https://www.ais.com.sg/", "intl", "International school"),
    ("Canadian International School Singapore", "https://www.cis.edu.sg/", "intl", "International school"),
    ("Dover Court International School", "https://www.dovercourt.edu.sg/", "intl", "International school"),
    ("Stamford American International School", "https://www.sais.edu.sg/", "intl", "International school"),
    ("Chatsworth International School", "https://www.chatsworth.com.sg/", "intl", "International school"),
    ("German European School Singapore", "https://www.gess.sg/", "intl", "International school"),
    ("Lycee Francais de Singapour", "https://www.lfs.edu.sg/", "intl", "French international"),
    ("Hwa Chong International School", "https://www.hcis.edu.sg/", "intl", "International school"),
    ("ACS International (Singapore)", "https://www.acsinternational.edu.sg/", "intl", "International school"),
    ("SJI International School", "https://www.sji-international.com.sg/", "intl", "International school"),
    ("Nexus International School Singapore", "https://www.nexus.edu.sg/", "intl", "International school"),
    ("Overseas Family School", "https://www.ofs.edu.sg/", "intl", "International school"),
    ("EtonHouse International School", "https://www.etonhouse.edu.sg/", "intl", "International school"),
    ("GIIS Singapore - Global Indian International School", "https://singapore.globalindianschool.org/", "intl", "International school"),
    ("DPS International School Singapore", "https://www.dpsinternational.com/", "intl", "International school"),
    ("NPS International School", "https://www.npsinternational.edu.sg/", "intl", "International school"),
    ("ISS International School", "https://www.iss.edu.sg/", "intl", "International school"),
    ("Swiss School in Singapore", "https://www.swiss-school.edu.sg/", "intl", "International school"),
    ("Hollandse School Limited", "https://www.hollandseschool.org/", "intl", "Dutch international"),
    ("Norwegian Supplementary School Singapore", "https://www.norwegianschool.com.sg/", "intl", "International school"),
    ("Korean International School Singapore", "https://www.singaporekorean.sch.sa/", "intl", "International school"),
    ("Waseda Shibuya Senior High School in Singapore", "https://www.waseda-shibuya.edu.sg/", "intl", "Japanese international"),
    ("Yokohama Senior High School Singapore", "https://www.yiss.edu.sg/", "intl", "Japanese international"),
    ("The Japanese School Singapore", "https://www.sjs.edu.sg/", "intl", "Japanese international"),
    ("Avondale Grammar School", "https://www.avondale.edu.sg/", "intl", "International school"),
    ("Insworld Institute", "https://www.insworld.edu.sg/", "intl", "International school"),
    ("Brighton College Singapore", "https://www.brightoncollege.edu.sg/", "intl", "International school"),
    ("XCL World Academy", "https://www.xwa.edu.sg/", "intl", "International school"),
    ("One World International School", "https://www.owis.org/", "intl", "International school"),
    ("Invictus International School", "https://www.invictus.edu.sg/", "intl", "International school"),
    ("Middleton International School", "https://www.middleton.edu.sg/", "intl", "International school"),

    # Special education / specialized
    ("Pathlight School", "https://www.pathlight.org.sg/", "other", "SPED autism"),
    ("Eden School", "https://www.edenschool.edu.sg/", "other", "SPED autism"),
    ("APSN Tanglin School", "https://www.apsn.org.sg/our-schools/tanglin-school/", "other", "SPED"),
    ("Canossian School", "https://canossian.moe.edu.sg/", "other", "SPED hearing impaired"),
    ("Lighthouse School", "https://www.lighthouse.edu.sg/", "other", "SPED visually impaired"),
    ("Grace Orchard School", "https://graceorchard.edu.sg/", "other", "SPED"),
    ("MINDS - Movement for the Intellectually Disabled of Singapore", "https://www.minds.org.sg/", "other", "SPED schools network"),
    ("Rainbow Centre", "https://www.rainbowcentre.org.sg/", "other", "SPED"),
    ("AWWA School", "https://www.awwa.org.sg/our-services/children-with-special-needs/awwa-school/", "other", "SPED"),
    ("Metta School", "https://www.mettaschool.edu.sg/", "other", "SPED"),
    ("Cerebral Palsy Alliance Singapore School", "https://cpas.org.sg/", "other", "SPED"),
    ("Beatty Secondary School Special Education", "https://beattysec.moe.edu.sg/", "other", "SPED programme"),

    # Other notable institutions
    ("Singapore Chinese Girls' School Junior Section", "https://scgs.moe.edu.sg/", "primary", "Through-train"),
    ("Civil Service College Singapore", "https://www.csc.gov.sg/", "other", "Public-service training"),
    ("Singapore Bible College", "https://www.sbc.edu.sg/", "other", "Theological"),
    ("Trinity Theological College", "https://www.ttc.edu.sg/", "other", "Theological"),
    ("Madrasah Aljunied Al-Islamiah", "https://www.aljunied.edu.sg/", "other", "Madrasah"),
    ("Madrasah Al-Irsyad Al-Islamiah", "https://www.al-irsyad.edu.sg/", "other", "Madrasah"),
    ("Madrasah Wak Tanjong Al-Islamiah", "https://www.waktanjong.edu.sg/", "other", "Madrasah"),
    ("Madrasah Alsagoff Al-Arabiah", "https://www.alsagoff.edu.sg/", "other", "Madrasah"),
    ("Madrasah Al-Maarif Al-Islamiah", "https://www.almaarif.edu.sg/", "other", "Madrasah"),
    ("Madrasah Al-Arabiah Al-Islamiah", "https://www.alarabiah.edu.sg/", "other", "Madrasah"),
]

for name, url, cat, notes in extra:
    u = norm_url(url)
    if u in seen_urls:
        continue
    seen_urls.add(u)
    rows.append((name, u, cat, notes))

# Sort: by category priority then name
cat_order = {'primary':1,'secondary':2,'jc':3,'poly':4,'uni':5,'intl':6,'other':7}
rows.sort(key=lambda r: (cat_order.get(r[2],99), r[0]))

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, 'w', encoding='utf-8', newline='') as f:
    w = csv.writer(f, lineterminator='\n')
    w.writerow(['name','url','category','notes'])
    for r in rows:
        w.writerow(r)

# Summary
from collections import Counter
c = Counter(r[2] for r in rows)
print('Total entries:', len(rows))
for k in ['primary','secondary','jc','poly','uni','intl','other']:
    print(f'  {k}: {c.get(k,0)}')
print('Wrote:', OUT)
