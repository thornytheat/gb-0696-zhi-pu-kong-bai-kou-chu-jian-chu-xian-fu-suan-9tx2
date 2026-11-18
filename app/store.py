import sqlite3, json, hashlib
from datetime import datetime, timezone
from app.config import DATA_DIR, RULE_VERSION
from app.errors import BusinessError
from app.amend import merge, amendment_digest

def connect():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA_DIR/"runs.sqlite", timeout=15)
    db.row_factory=sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT '待复核', created TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS versions (run_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(run_id,revision))")
    db.execute("CREATE TABLE IF NOT EXISTS amendments (run_id TEXT NOT NULL, digest TEXT NOT NULL, record TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(run_id,digest))")
    return db

def _now(): return datetime.now(timezone.utc).isoformat()

def _row_to_item(row):
    item=dict(row);item["result"]=json.loads(item["result"]);item["rule_version"]=item["result"].get("rule_version","2026.11.18.1")
    return item

def _snapshot(db,item):
    snapshot=hashlib.sha256(json.dumps(item["result"],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    db.execute("INSERT OR IGNORE INTO versions(run_id,revision,digest,result,created) VALUES(?,?,?,?,?)",
               (item["id"],item["revision"],snapshot,json.dumps(item["result"],ensure_ascii=False),_now()))

def save(text, result):
    digest=hashlib.sha256((RULE_VERSION+"\n"+text).encode()).hexdigest()
    ident=digest[:16]
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,digest,result,created) VALUES(?,?,?,?)",(ident,digest,json.dumps(dict(result,rule_version=RULE_VERSION),ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
        row=db.execute("SELECT * FROM runs WHERE id=?",(ident,)).fetchone()
        _snapshot(db,_row_to_item(row))
    return get(ident)

def get(ident):
    with connect() as db:
        row=db.execute("SELECT * FROM runs WHERE id=?",(ident,)).fetchone()
    if row is None: raise BusinessError("NOT_FOUND","批次不存在",404)
    return _row_to_item(row)

def versions(ident):
    with connect() as db:
        rows=db.execute("SELECT run_id,revision,digest,created FROM versions WHERE run_id=? ORDER BY revision",(ident,)).fetchall()
    return [dict(r) for r in rows]

def history():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT id,revision,decision,created FROM runs ORDER BY created DESC LIMIT 40")]

def review(ident,revision,decision):
    if not isinstance(decision,str) or decision not in {"通过","退回"}: raise BusinessError("DECISION","复核决定无效")
    with connect() as db:
        row=db.execute("SELECT * FROM runs WHERE id=?",(ident,)).fetchone()
        if row is None: raise BusinessError("NOT_FOUND","批次不存在",404)
        current=_row_to_item(row)
        if revision!=current["revision"]: raise BusinessError("STALE","版本已更新，请刷新",409)
        if decision=="通过" and current["result"].get("blocked"):
            raise BusinessError("BLOCKED","存在阻断项，请先修正数据",409)
        result=current["result"]
        # 通过即锁定该版本全部样品结论；退回是显式决定，样品回到待复核
        for r in result.get("final",[]):
            if decision=="通过": r["review"]="已复核"
            else: r.pop("review",None)
        _snapshot(db,current)
        db.execute("UPDATE runs SET result=?,revision=revision+1,decision=? WHERE id=? AND revision=?",
                   (json.dumps(result,ensure_ascii=False),decision,ident,revision))
    return get(ident)

def amend(ident,text,revision=None):
    """把补交行并入同一批次。同一份补交重试不新增批次，失败时不改动旧版结果。"""
    if not isinstance(text,str): raise BusinessError("TEXT","输入须为文本")
    digest=amendment_digest(text)
    with connect() as db:
        row=db.execute("SELECT * FROM runs WHERE id=?",(ident,)).fetchone()
        if row is None: raise BusinessError("NOT_FOUND","批次不存在",404)
        current=_row_to_item(row)
        prior=db.execute("SELECT record FROM amendments WHERE run_id=? AND digest=?",(ident,digest)).fetchone()
        if prior is not None:  # 重放：不新增版本、不重复合并（幂等优先于版本校验）
            item=get(ident);item["amendment"]=json.loads(prior["record"]);item["replayed"]=True
            return item
        if revision is not None and revision!=current["revision"]:
            raise BusinessError("STALE","版本已更新，请刷新",409)
        try:
            result,record=merge(current["result"],text)
        except BusinessError:
            raise
        except Exception:
            raise BusinessError("MERGE_FAILED","合并失败，已保留旧版结果")
        changed=record.get("effective",0)>0 or record["counts"]["停在原结论"]>0
        db.execute("INSERT OR IGNORE INTO amendments(run_id,digest,record,created) VALUES(?,?,?,?)",
                   (ident,digest,json.dumps(record,ensure_ascii=False),_now()))
        if not changed:  # 无实质变化（仅内容一致/仍隔离），不产生新版本
            item=get(ident);item["amendment"]=record;item["replayed"]=False
            return item
        _snapshot(db,current)
        decision="待复核" if record["counts"]["已生效"] else current["decision"]
        updated=db.execute("UPDATE runs SET result=?,revision=revision+1,decision=? WHERE id=? AND revision=?",
                           (json.dumps(result,ensure_ascii=False),decision,ident,current["revision"])).rowcount
        if not updated: raise BusinessError("STALE","版本已更新，请刷新",409)
    item=get(ident);item["amendment"]=record;item["replayed"]=False
    return item
