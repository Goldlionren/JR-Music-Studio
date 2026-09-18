"""Explicit, immutable per-command songcraft materials; no global Skill loading."""
import json
import re
from pathlib import Path
from .store import canonical, sha, require

ROOT = Path(__file__).resolve().parent.parent/'integrations/hermes/jr-chinese-songcraft'
ROUTES = {
    'journey': ('人生行旅', ('R01','R02')),
    'return': ('东方等待', ('R03',)),
    'farewell': ('现代离别', ('R04',)),
    'companionship': ('希望陪伴', ('R05',)),
    'metaphor': ('关系隐喻', ('R06',)),
    'perspectives': ('人物互照', ('R07',)),
}

def catalog():
    basic = [dict(id='professional',label='专业编曲 + 作词 · jtydhr88（主要方法）'),dict(id='none',label='旧基线对照 · 不启用技能')]
    if not (ROOT/'references/craft.md').is_file():
        return basic
    return basic + [
            dict(id='foundation',label='旧方法对照 · 中文创作基础')] + [
        dict(id=k,label='旧方法对照 · '+v[0]) for k,v in ROUTES.items()]

def validate(selection):
    require(isinstance(selection,str) and selection in {'none','foundation','professional',*ROUTES}, 'INVALID_SONGCRAFT_SELECTION')

def build(selection):
    validate(selection)
    if selection=='professional':
        from .professional_skills import bundle
        return bundle()
    craft=(ROOT/'references/craft.md').read_text(encoding='utf-8')
    # The foundation arm must not receive reference-derived routes or cards.
    foundation=craft.split('## 六条可选路线')[0]+'## 审稿问题'+craft.split('## 审稿问题',1)[1]
    materials={'foundation':foundation,'contract':(ROOT/'references/jr-contract.md').read_text(encoding='utf-8')}
    if selection in ROUTES:
        label, ids=ROUTES[selection]
        cards=(ROOT/'references/source-cards.md').read_text(encoding='utf-8')
        sections=re.split(r'(?m)(?=^## )',cards)
        materials['selected_route']='仅采用 '+label+' 路线。分析卡是有限文字观察与创作提案，音频规律未测量。不要复制原作词句或旋律；当次制作人要求优先。'
        materials['selected_cards']='\n'.join(s for s in sections if any(s.startswith('## '+i+' ') for i in ids))
        require(all('## '+i+' ' in materials['selected_cards'] for i in ids),'SONGCRAFT_PACKAGE_INVALID')
    return dict(schema_version='songcraft-materials/1',name='jr-chinese-songcraft',version='0.1.0',selection=selection,materials=materials)

def freeze(store,db,selection):
    if selection=='none': return None
    bundle=build(selection)
    digest=store._blob(db,canonical(bundle))
    return dict(name=bundle['name'],version=bundle['version'],selection=selection,
                label=next(v['label'] for v in catalog() if v['id']==selection),sha256=digest)

def resolve(store,command):
    frozen=command.get('songcraft')
    if not frozen: return None
    with store.connect() as db:
        row=db.execute('SELECT content FROM blobs WHERE hash=?',(frozen['sha256'],)).fetchone()
    require(row is not None and sha(row[0])==frozen['sha256'],'SONGCRAFT_SNAPSHOT_INVALID')
    bundle=json.loads(row[0])
    require(all(bundle[k]==frozen[k] for k in ('name','version','selection')),'SONGCRAFT_SNAPSHOT_INVALID')
    return bundle
