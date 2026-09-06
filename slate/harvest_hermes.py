"""One-shot harvest: user's own Slate submissions + discussion posts
into coop-job-assistant data/writing/slate/ for voice distillation.

Run with hermes venv (httpx + session):
  ~/Projects/hermes/.venv-local/bin/python harvest_slate_writing.py
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "/Users/sekun/Projects/hermes")
from slate.client import SlateClient

OUT = Path("/Users/sekun/Documents/GitHub/coop-job-assistant/data/writing/slate")


def slug(s):
    return (re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "course")[:60]


def html2text(h):
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"<(p|div|li|h\d)[^>]*>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", "", h)
    import html as H
    return H.unescape(h).strip()


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    async with SlateClient() as c:
        uid = await c._get_user_id()
        print("user:", uid)
        raw = await c._try_get(
            "/d2l/api/lp/1.0/enrollments/myenrollments/",
            pageSize=200, orgUnitTypeId=3) or {}
        courses = []
        for it in (raw.get("Items", []) if isinstance(raw, dict) else []):
            o = it.get("OrgUnit", {})
            if o.get("Id"):
                courses.append({"id": str(o["Id"]),
                                "name": o.get("Name", ""),
                                "code": o.get("Code", "")})
        print("courses:", len(courses))
        for course in courses:
            cd = OUT / slug(course["code"] or course["name"])
            cd.mkdir(parents=True, exist_ok=True)
            # --- dropbox ---
            folders = await c._try_get(
                f"/d2l/api/le/1.0/{course["id"]}/dropbox/folders/", pageSize=200) or {}
            for f in (folders if isinstance(folders, list) else folders.get("Objects", [])):
                fid = f.get("Id")
                subs = await c._try_get(
                    f"/d2l/api/le/1.0/{course["id"]}/dropbox/folders/{fid}/submissions/")
                items = subs if isinstance(subs, list) else (subs or {}).get("Objects", [])
                for it in items:
                    ent = it.get("Entity") or {}
                    try:
                        eid = int(ent.get("EntityId"))
                    except Exception:
                        eid = None
                    if uid is not None and eid != uid:
                        continue
                    for s in it.get("Submissions") or []:
                        sid = s.get("Id")
                        # files are listed at record level — download direct
                        for fl in s.get("Files") or []:
                            fid2 = fl.get("FileId")
                            fn = re.sub(r"[^\w.\- ]", "_", fl.get("FileName", f"file-{fid2}"))
                            url = ("https://slate.sheridancollege.ca/d2l/api/le/1.0/"
                                   f"{course['id']}/dropbox/folders/{fid}/submissions/{sid}/files/{fid2}")
                            try:
                                r = await c._client.get(url)
                                r.raise_for_status()
                                (cd / fn).write_bytes(r.content)
                                manifest.append(str(cd / fn))
                                print("  file:", fn, len(r.content))
                            except Exception as e:
                                print("  file-skip:", fn, e)
                        # text-entry submissions via detail endpoint
                        try:
                            det = await c._try_get(
                                f"/d2l/api/le/1.0/{course['id']}/dropbox/folders/{fid}/submissions/{sid}")
                        except Exception:
                            det = None
                        txt = html2text((det or {}).get("SubmissionText") or "")
                        if txt:
                            p = cd / f"{slug(f.get('Name','submission'))}-text.txt"
                            p.write_text(f"# {course['name']}\n# {f.get('Name')}\n\n{txt}\n")
                            manifest.append(str(p))
                            print("  text:", p.name, len(txt))
            # --- discussions ---
            forums = await c._try_get(
                f"/d2l/api/le/1.0/{course["id"]}/discussions/forums/", pageSize=200) or {}
            for fm in (forums if isinstance(forums, list) else forums.get("Objects", [])):
                topics = await c._try_get(
                    f"/d2l/api/le/1.0/{course["id"]}/discussions/forums/{fm.get('ForumId')}/topics/",
                    pageSize=200) or {}
                for t in (topics if isinstance(topics, list) else topics.get("Objects", [])):
                    posts = await c._try_get(
                        f"/d2l/api/le/1.0/{course["id"]}/discussions/forums/{fm.get('ForumId')}"
                        f"/topics/{t.get('TopicId')}/posts/", pageSize=200) or {}
                    mine = []
                    for p in (posts if isinstance(posts, list) else posts.get("Objects", [])):
                        for post in ([p] + (p.get("Replies") or [])):
                            try:
                                poster = int(post.get("PostingUserId"))
                            except Exception:
                                poster = None
                            if uid is not None and poster != uid:
                                continue
                            msg = post.get("Message") or {}
                            b = html2text(msg.get("Html", "") or msg.get("Text", "")
                                          if isinstance(msg, dict) else msg)
                            if b:
                                mine.append(b)
                    if mine:
                        p = cd / f"{slug(t.get('Name','topic'))}-posts.md"
                        p.write_text(f"# {course["name"]} — {t.get('Name')}\n\n" +
                                     "\n\n---\n\n".join(mine))
                        manifest.append(str(p))
                        print("  posts:", p.name, len(mine))
    print(f"\nHARVESTED {len(manifest)} item(s)")
    for m in manifest:
        print(" ", m)


asyncio.run(main())
