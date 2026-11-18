SEP="\x1f"

def row_key(row):
    return SEP.join(str(row.get(k,"")) for k in ("batch","analyte","sample_id"))

def key_text(key):
    if isinstance(key,(tuple,list)):
        return SEP.join(str(x) for x in key)
    return str(key)
