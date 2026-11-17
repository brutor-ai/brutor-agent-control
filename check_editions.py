# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Check editions.yaml against the code: every path, route prefix, table,
feature flag, nav id and docs page it names must exist.

    python3 check_editions.py [--layout public|private] [--print-layout]

Needs PyYAML. Exits 1 if anything is not found. editions.yaml is the file
beside this one; every path in it is relative to its component's directory,
and the component directories come from one mapping, LAYOUTS below:

  public   the Community repository: this file at its root, beside
           core-gateway/, control-plane/ and admin-console/. The Enterprise
           trees and the docs site are not there: every check that needs
           them is skipped and counted, every check of the Community side runs.
  private  the sibling repositories, each with its enterprise/ directory, and
           the docs site.

Without --layout the layout is the one whose directories are beside this file.
"""
import yaml, json, os, re, sys, argparse
HERE=os.path.dirname(os.path.abspath(__file__))
# The one mapping of component directories. `base` is relative to this file;
# `enterprise` is the directory of each component that holds its Enterprise
# tree (None: not in this layout); `docs` is the docs site (None: not here).
LAYOUTS={
    "public":{"base":".","core_gateway":"core-gateway","control_plane":"control-plane","admin_console":"admin-console","docs":None,"enterprise":None},
    "private":{"base":"../../..","core_gateway":"brutor-gateway-core","control_plane":"brutor-gateway-control","admin_console":"brutor-gateway-admin","docs":"brutor-ai-docs","enterprise":"enterprise"},
}
def layout_roots(name,here=HERE):
    """{component key: absolute directory} of a layout, plus `docs` and `enterprise`."""
    lay=LAYOUTS[name]; base=os.path.abspath(os.path.join(here,lay["base"]))
    out={k:os.path.join(base,lay[k]) for k in ("core_gateway","control_plane","admin_console")}
    out["docs"]=os.path.join(base,lay["docs"]) if lay["docs"] else None
    out["enterprise"]=lay["enterprise"]
    return out
def detect_layout(here=HERE):
    found=[n for n in LAYOUTS if all(os.path.isdir(d) for k,d in layout_roots(n,here).items() if k in ("core_gateway","control_plane","admin_console"))]
    return found[0] if len(found)==1 else None
_ap=argparse.ArgumentParser(description="Check editions.yaml against the code.")
_ap.add_argument("--layout",choices=sorted(LAYOUTS))
_ap.add_argument("--print-layout",action="store_true",help="print the component directories as JSON and exit")
_args=_ap.parse_args()
LAYOUT=_args.layout or detect_layout()
if LAYOUT is None: sys.exit("check_editions: no layout given and none found beside this file (public: core-gateway/, control-plane/, admin-console/)")
ROOTS=layout_roots(LAYOUT)
if _args.print_layout:
    print(json.dumps({"layout":LAYOUT,**ROOTS},indent=1,sort_keys=True)); raise SystemExit(0)
CORE,CTL,ADM=ROOTS["core_gateway"],ROOTS["control_plane"],ROOTS["admin_console"]
EROOT=ROOTS["enterprise"]            # "enterprise" in the private layout, None in the public one
ENT=EROOT is not None
e=yaml.safe_load(open(os.path.join(HERE,"editions.yaml")))
bad=[]
skipped=[]
def absent(rel):
    """True for a component path under the Enterprise directory when this layout has none: not checked."""
    if ENT or rel.split("/")[0]!="enterprise": return False
    skipped.append(rel); return True
def skip(what):
    skipped.append(what)
for _k,_d in (("core_gateway",CORE),("control_plane",CTL),("admin_console",ADM)):
    if not os.path.isdir(_d): bad.append(("component directory not found",LAYOUT,_k,_d))
    # A layout with Enterprise trees that are not there would check nothing of them and pass.
    elif ENT and not os.path.isdir(f"{_d}/{EROOT}"): bad.append(("Enterprise directory not found",LAYOUT,_k,f"{_d}/{EROOT}"))
if bad:
    print("NOT FOUND:",bad); raise SystemExit(1)
def flat(x):
    for v in (x.values() if isinstance(x,dict) else x):
        if isinstance(v,(list,dict)): yield from flat(v)
        else: yield v
for p in flat(e["control_plane"]["enterprise_modules"]):
    if absent(p): continue
    if not os.path.exists(f"{CTL}/{p}"): bad.append(("control path",p))
# The Community modules that may read or delete rows
# of Enterprise-only tables. Each exists, is not itself an Enterprise module,
# and has a reason written next to it. (The control plane's table guard,
# tests/test_edition_table_guard.py, holds the code to this list.)
readers=e["control_plane"].get("enterprise_table_readers",{})
ent_mods=list(flat(e["control_plane"]["enterprise_modules"]))
for p,why in readers.items():
    if not os.path.exists(f"{CTL}/{p}"): bad.append(("control table reader",p))
    if any(p==q or (q.endswith("/") and p.startswith(q)) for q in ent_mods): bad.append(("control table reader is an enterprise module",p))
    if not isinstance(why,str) or len(why.split())<5: bad.append(("control table reader without a reason",p))
print("community modules that may read enterprise-only tables:",len(readers))
# The Enterprise composition's control-plane document: the one editions.yaml names for it.
_snap=e["admin_console"]["api_contract"]["enterprise"]["control_document"]
spec=None if absent(_snap) else json.load(open(f"{CTL}/{_snap}"))["paths"]
if spec is not None:
    n=0
    for pf in flat(e["control_plane"]["enterprise_route_prefixes"]):
        hit=[p for p in spec if p==pf or p.startswith(pf+"/")]
        if not hit: bad.append(("control prefix",pf))
        n+=sum(1 for p in hit for m in spec[p] if m in("get","post","put","patch","delete"))
    total_ops=sum(1 for p in spec for m in spec[p] if m in("get","post","put","patch","delete"))
    print("enterprise control-plane operations under whole prefixes:",n,"of",total_ops)
# The licence gate: the flag every route under a prefix needs. The
# table of the boundary file is the table the Enterprise package gates its
# routers from, and holds every prefix of a feature that has a licence flag.
import ast
_gate=e["control_plane"].get("licence_gated_route_prefixes") or {}
_tff=e["control_plane"]["tenant_feature_flags"]
_gate_flat={pf:flag for flag,pfs in _gate.items() for pf in pfs}
_flagged=e["control_plane"].get("licence_flagged_features") or []
for _f in _flagged:
    for pf in e["control_plane"]["enterprise_route_prefixes"].get(_f,[]):
        if pf not in _gate_flat: bad.append(("control prefix of a licensed feature has no licence flag",_f,pf))
for pf in _gate_flat:
    if not any(pf in e["control_plane"]["enterprise_route_prefixes"].get(_f,[]) for _f in _flagged):
        bad.append(("licence-gated prefix is not a prefix of a licensed feature",pf))
for flag in _gate:
    if flag not in _tff["enterprise"]+_tff["community"]+list(_tff.get("split",{})): bad.append(("licence flag is not a tenant feature flag",flag))
_gate_py=f"{CTL}/enterprise/brutor_enterprise/licence/features.py"
if absent("enterprise/brutor_enterprise/licence/features.py"): pass
elif os.path.exists(_gate_py):
    _code={}
    for _n in ast.parse(open(_gate_py).read()).body:
        if isinstance(_n,ast.AnnAssign) and getattr(_n.target,"id","")=="GATED_ROUTE_PREFIXES":
            _code=ast.literal_eval(_n.value)
    if _code!=_gate_flat: bad.append(("licence gate table in the code differs from licence_gated_route_prefixes",sorted(set(_code.items())^set(_gate_flat.items()))))
else: bad.append(("control licence gate",_gate_py))
print("control licence-gated route prefixes:",len(_gate_flat),"for",len(_gate),"licence flags")
for s in e["control_plane"]["split"]:
    if spec is not None and not any(p.startswith(s["prefix"]) for p in spec): bad.append(("split prefix",s["prefix"]))
    # A prefix split into a Community and an Enterprise router: the
    # unsplit module is gone, the Community module exists, and the operations
    # listed as Community exist in the published schema. Which router serves
    # what, and each router's tag, is checked with the registry below.
    if "removed_module" in s and os.path.exists(f"{CTL}/{s['removed_module']}"): bad.append(("split: unsplit module still present",s["removed_module"]))
    if "community_module" in s and not os.path.exists(f"{CTL}/{s['community_module']}"): bad.append(("split: community module",s["community_module"]))
    for op in (s.get("community_operations",[]) if spec is not None else []):
        m,p=op.split(" ")
        if m.lower() not in spec.get(p,{}): bad.append(("split: community operation not in the schema",op))
schema=open(f"{CORE}/tests/schema/schema.sql").read()
tables=set(re.findall(r"CREATE TABLE (?:public\.)?(\w+)",schema))
ent=list(flat(e["control_plane"]["enterprise_only_tables"]))
for t in ent:
    if t not in tables: bad.append(("table",t))
print("enterprise-only tables:",len(ent),"of",len(tables))
src=open(f"{CTL}/app/constants/tenant.py").read()
flags=e["control_plane"]["tenant_feature_flags"]
allf=flags["enterprise"]+flags["community"]+list(flags["split"])
for f in allf:
    if f'"{f}"' not in src and f"'{f}'" not in src: bad.append(("flag",f))
print("feature flags listed:",len(allf))
# The route chain is assembled in src/app.rs; main.rs calls it.
main=open(f"{CORE}/src/app.rs").read()
# A feature that is an extension mounts its own prefixes: they
# are in the extension's module and no longer in src/app.rs.
ext_built=e["core_gateway"].get("extensions_built",{})
for feat,pfs in e["core_gateway"]["enterprise_route_prefixes"].items():
    for pf in pfs:
        if feat in ext_built and absent(ext_built[feat]["module"]):
            if f'"{pf}"' in main: bad.append(("core prefix of an extension still in src/app.rs",feat,pf))
        elif feat in ext_built:
            mp=f"{CORE}/{ext_built[feat]['module']}"
            # The literal is in the extension's module, or in a module of the
            # same feature that the extension mounts through a constant
            # (the connector icons, enterprise/src/routes/connector_icons.rs).
            mods=[mp]+[f"{CORE}/{m}" for m in e["core_gateway"].get("enterprise_modules",{}).get(feat,[]) if isinstance(m,str)]
            if not os.path.exists(mp) or not any(os.path.isfile(m) and f'"{pf}"' in open(m).read() for m in mods): bad.append(("core prefix not mounted by its extension",feat,pf))
            if f'"{pf}"' in main: bad.append(("core prefix of an extension still in src/app.rs",feat,pf))
        elif f'"{pf}"' not in main: bad.append(("core prefix",pf))
# The core's route-table guard (tests/integration/mounted_routes.rs,
# ENTERPRISE_ROUTE_PREFIXES) names every Enterprise prefix of this manifest,
# so the guard and the manifest cannot drift apart.
_core_route_guard=open(f"{CORE}/tests/integration/mounted_routes.rs").read()
for feat,pfs in e["core_gateway"]["enterprise_route_prefixes"].items():
    for pf in pfs:
        if f'    "{pf}",' not in _core_route_guard: bad.append(("core route-table guard does not list the Enterprise prefix",feat,pf))
for m in e["core_gateway"]["moved_to_community"]:
    old=m["from"].split(" ")[0]; new=m["to"]
    # the old mount is gone (an exact nest at the old prefix), the new one is there
    if f'.nest("{old}",' in main: bad.append(("core move: old mount still present",old))
    if f'"{new}"' not in main: bad.append(("core move: new mount missing",new))
    if not os.path.exists(f"{CORE}/{m['module']}"): bad.append(("core move: module",m["module"]))
for p in flat(e["core_gateway"]["enterprise_modules"]):
    if (p.startswith("src/") or (p.startswith("enterprise/") and not absent(p))) and not os.path.exists(f"{CORE}/{p}"): bad.append(("core path",p))
    # A feature that moved to the Enterprise workspace left nothing behind in the Community crate.
    if p.startswith("enterprise/src/") and os.path.exists(f"{CORE}/src/services/{os.path.basename(p.rstrip('/'))}"): bad.append(("core Enterprise module has a twin under src/services",p))
# --- Core extension hooks that exist: the trait and its
# default live in the Community module; a state built without an extension
# holds the default; the Enterprise type is defined in its own module and no
# route module names it.
core_state=open(f"{CORE}/src/services/mod.rs").read()
for hook,h in e["core_gateway"].get("hooks_built",{}).items():
    if hook not in e["core_gateway"]["hooks"]: bad.append(("built hook not in hooks",hook))
    for k in ("module","enterprise_impl"):
        if not absent(h[k]) and not os.path.exists(f"{CORE}/{h[k]}"): bad.append(("core hook path",hook,h[k]))
    if os.path.exists(f"{CORE}/{h['module']}"):
        src=open(f"{CORE}/{h['module']}").read()
        if f"pub trait {hook}" not in src: bad.append(("core hook trait not in its module",hook))
        if f"pub struct {h['default']}" not in src: bad.append(("core hook default not in its module",hook,h["default"]))
        if h["enterprise_type"] in src: bad.append(("community hook module names the enterprise type",hook))
    if f"::{h['default']})" not in core_state and f"::{h['default']}," not in core_state: bad.append(("core state does not start with the hook default",hook,h["default"]))
    if not absent(h["enterprise_impl"]) and os.path.exists(f"{CORE}/{h['enterprise_impl']}") and f"pub struct {h['enterprise_type']}" not in open(f"{CORE}/{h['enterprise_impl']}").read(): bad.append(("core hook enterprise type not in its module",hook))
    feat=e["core_gateway"]["hooks"].get(hook)
    if h["enterprise_impl"] not in list(flat(e["core_gateway"]["enterprise_modules"].get(feat,[]))): bad.append(("core hook implementation not listed under its feature",hook,feat))
    for rf in sorted(os.listdir(f"{CORE}/src/routes")):
        if rf.endswith(".rs") and h["enterprise_type"] in open(f"{CORE}/src/routes/{rf}").read(): bad.append(("route names a hook's enterprise type",hook,rf))
print("core hooks built:",len(e["core_gateway"].get("hooks_built",{})),"of",len(e["core_gateway"]["hooks"]))
audit=open(f"{CORE}/src/services/audit_chain.rs").read()+open(f"{CORE}/src/models/db.rs").read()
for f in e["core_gateway"]["evidence_fields_kept"]:
    if f not in audit: bad.append(("evidence field",f))
# The navigation is defined in src/app/navigation.tsx, which is the
# Community navigation: an Enterprise entry is
# placed by the Enterprise extension (admin_console.extension.navigation) and
# is in that file only; a Community entry is in the Community file only.
ac=e["admin_console"]
AX=ac["extension"]
for k in ("entry","screens","navigation","slots","api_modules","hook_modules","hook"):
    if absent(AX[k]):
        if not AX[k].startswith(AX["root"]+"/"): bad.append(("admin extension file outside its root",k,AX[k]))
    elif not os.path.isfile(f"{ADM}/{AX[k]}"): bad.append(("admin extension file",k,AX[k]))
    elif not AX[k].startswith(AX["root"]+"/"): bad.append(("admin extension file outside its root",k,AX[k]))
com_nav=open(f"{ADM}/src/app/navigation.tsx").read()
ent_nav=None if absent(AX["navigation"]) else open(f"{ADM}/{AX['navigation']}").read()
side=com_nav+(ent_nav or "")
for i in list(flat(ac["enterprise_nav_ids"]))+ac["tenant_console_nav_ids"]:
    if ent_nav is not None and f"id: '{i}'" not in ent_nav: bad.append(("nav id",i))
    if f"id: '{i}'" in com_nav: bad.append(("enterprise nav id in the Community navigation",i))
for i in ac["tenant_console_also_mounts"]:
    if f"id: '{i}'" not in com_nav: bad.append(("nav id",i))
    if ent_nav is not None and f"id: '{i}'" in ent_nav: bad.append(("community nav id in the Enterprise navigation",i))
# Every entry the extension places is a listed one: nothing Enterprise is in the sidebar unlisted.
_listed_nav=set(flat(ac["enterprise_nav_ids"]))|set(ac["tenant_console_nav_ids"])
for i in re.findall(r"\bid: '([^']+)'",ent_nav or ""):
    if i not in _listed_nav: bad.append(("nav id placed by the extension is not listed in editions.yaml",i))
# Two entry points: the Community one composes the console
# with no extension and imports nothing of the Enterprise tree; the
# Enterprise one (extension.app_entry) passes the extension entry to the
# Community console's mount. Each package has the files its build needs.
_idx=open(f"{ADM}/src/index.tsx").read()
if "mountConsole()" not in _idx or AX["root"]+"/" in _idx: bad.append(("src/index.tsx is not the Community entry point: mountConsole() with no extension, nothing of the Enterprise tree",))
for _k in ("app_entry","package","index_html","vite_config","vitest_config","tsconfig","eslint_config","dockerfile"):
    if absent(AX[_k]):
        if not AX[_k].startswith(AX["root"]+"/"): bad.append(("admin Enterprise package file outside its root",_k,AX[_k]))
    elif not os.path.isfile(f"{ADM}/{AX[_k]}"): bad.append(("admin Enterprise package file",_k,AX[_k]))
    elif not AX[_k].startswith(AX["root"]+"/"): bad.append(("admin Enterprise package file outside its root",_k,AX[_k]))
if os.path.isfile(f"{ADM}/{AX['app_entry']}"):
    _eidx=open(f"{ADM}/{AX['app_entry']}").read()
    if "mountConsole([enterpriseExtension])" not in _eidx or f"from './{os.path.basename(AX['entry'])[:-4]}'" not in _eidx:
        bad.append(("the Enterprise entry point does not mount the console with the extension entry",AX["app_entry"]))
# No file under src/ imports from the extension's root: not the application
# (every file of an Enterprise feature is outside src/, and the Community
# entry point composes no extension), and not a test or a test helper. The admin suite holds the same line twice, by an ESLint rule
# and by src/app/__tests__/editionBoundary.test.ts; there is no allow-list.
for _dp,_dn,_fn in os.walk(f"{ADM}/src"):
    _dn[:]=[d for d in _dn if d!="node_modules"]
    for _f in _fn:
        if not _f.endswith((".ts",".tsx")) or _f.endswith(".d.ts"): continue
        _rel=os.path.relpath(os.path.join(_dp,_f),f"{ADM}")
        _src=open(os.path.join(_dp,_f)).read()
        for _spec in re.findall(r"""(?:from\s+|import\(\s*|import\s+|vi\.mock\(\s*|glob(?:<[^(]*>)?\(\s*)['"]((?:\.\./)+%s/[^'"]+)['"]"""%re.escape(AX["root"]),_src):
            if os.path.normpath(os.path.join(os.path.dirname(_rel),_spec)).split(os.sep)[0]!=AX["root"]: continue
            bad.append(("community file loads from the Enterprise tree",_rel,_spec))
# The Community package names no Enterprise-only package, and the Community
# build configuration has neither the obfuscator nor the Monaco chunk; the
# Enterprise package declares them.
import json as _json
_cpkg=_json.load(open(f"{ADM}/package.json"))
_epkg=_json.load(open(f"{ADM}/{AX['package']}")) if os.path.isfile(f"{ADM}/{AX['package']}") else {}
_cdeps={**_cpkg.get("dependencies",{}),**_cpkg.get("devDependencies",{})}
_edeps={**_epkg.get("dependencies",{}),**_epkg.get("devDependencies",{})}
for _d in AX["enterprise_only_packages"]:
    if _d in _cdeps: bad.append(("Enterprise-only package in the Community package.json",_d))
    if ENT and _d not in _edeps: bad.append(("Enterprise-only package not declared by the Enterprise package",_d))
_lint=re.search(r"const ENTERPRISE_ONLY_PACKAGES = \[([^\]]*)\]",open(f"{ADM}/eslint.config.js").read())
if not _lint or sorted(re.findall(r"'([^']+)'",_lint.group(1)))!=sorted(AX["enterprise_only_packages"]): bad.append(("the Community lint rule does not forbid exactly the Enterprise-only packages",))
if ENT and _epkg.get("name")!=AX["package_name"]: bad.append(("admin Enterprise package name",_epkg.get("name")))
if ENT and _epkg.get("version")!=_cpkg.get("version"): bad.append(("admin package versions differ (the two packages are versioned in lockstep)",_cpkg.get("version"),_epkg.get("version")))
_cvite=open(f"{ADM}/vite.config.ts").read()
for _w in ("obfuscat","monaco"):
    if _w in re.sub(r"//.*","",_cvite).lower(): bad.append(("the Community vite configuration names",_w))
# The Community bundle scan reads its own copy of the lists below; the copy is these lists.
_deny=_json.load(open(f"{ADM}/{ac['bundle_scan']['denylist']}"))
if not os.path.isfile(f"{ADM}/{ac['bundle_scan']['script']}"): bad.append(("admin bundle scan script",ac["bundle_scan"]["script"]))
if sorted(_deny["routePrefixes"])!=sorted(flat(e["control_plane"]["enterprise_route_prefixes"])): bad.append(("admin bundle denylist route prefixes differ from control_plane.enterprise_route_prefixes",sorted(set(_deny["routePrefixes"])^set(flat(e["control_plane"]["enterprise_route_prefixes"])))))
if sorted(_deny["navIds"])!=sorted(list(flat(ac["enterprise_nav_ids"]))+ac["tenant_console_nav_ids"]): bad.append(("admin bundle denylist nav ids differ from enterprise_nav_ids and tenant_console_nav_ids",sorted(set(_deny["navIds"])^(set(flat(ac["enterprise_nav_ids"]))|set(ac["tenant_console_nav_ids"])))))
if sorted(_deny.get("sharedNavIds",{}))!=sorted(ac["bundle_scan"]["shared_nav_ids"]): bad.append(("admin bundle denylist shared nav ids differ from bundle_scan.shared_nav_ids",sorted(_deny.get("sharedNavIds",{}))))
for _i,_row in (_deny.get("sharedNavIds",{}).items() if ent_nav is not None else []):
    if f"id: '{_i}', label: '{_row['label']}'" not in re.sub(r"\s+"," ",ent_nav): bad.append(("admin bundle denylist label is not the Enterprise entry's",_i,_row["label"]))
# The API contract per edition: each package of the Admin
# Console has the route list and the generated types of its own edition's
# control-plane document. The admin suite holds its client to the lists; here
# the lists are held to the documents and to each other.
_api=ac["api_contract"]
_docs_src=open(f"{ADM}/{_api['documents']}").read() if os.path.isfile(f"{ADM}/{_api['documents']}") else ""
if not _docs_src: bad.append(("admin api contract documents file",_api["documents"]))
_api_routes={}
for _ed in ("community","enterprise"):
    _row=_api[_ed]
    _have=not any(absent(_row[_k]) for _k in ("routes","types"))
    for _k in ("routes","types"):
        if _have and not os.path.isfile(f"{ADM}/{_row[_k]}"): bad.append(("admin api contract file",_ed,_k,_row[_k]))
        if _row[_k].startswith(AX["root"]+"/")!=(_ed=="enterprise"): bad.append(("admin api contract file in the wrong package",_ed,_k,_row[_k]))
        if _have and f"'{_row[_k]}'" not in _docs_src: bad.append(("admin api contract documents file does not name",_ed,_k,_row[_k]))
    if _have and f"'{_row['control_document']}'" not in _docs_src: bad.append(("admin api contract documents file does not name",_ed,"control_document",_row["control_document"]))
    if not _have: continue
    _doc_path=f"{CTL}/{_row['control_document']}"
    if not os.path.isfile(_doc_path): bad.append(("admin api contract control document",_ed,_row["control_document"])); continue
    _ops=sorted(f"{_m.upper()} {_p}" for _p,_item in _json.load(open(_doc_path))["paths"].items() for _m in _item if _m in ("get","put","post","delete","patch","head","options"))
    _api_routes[_ed]=_json.load(open(f"{ADM}/{_row['routes']}")) if os.path.isfile(f"{ADM}/{_row['routes']}") else []
    if _api_routes[_ed]!=_ops: bad.append(("admin vendored route list is not the operations of its control document",_ed,len(_api_routes[_ed]),len(_ops)))
    _typed=re.findall(r'^    "(/[^"]*|/)": \{$',open(f"{ADM}/{_row['types']}").read().split("\nexport type webhooks")[0],re.M) if os.path.isfile(f"{ADM}/{_row['types']}") else []
    if sorted(_typed)!=sorted({_o.split(" ",1)[1] for _o in _ops}): bad.append(("admin generated types do not have the paths of their control document",_ed,len(_typed)))
_cp_prefixes=list(flat(e["control_plane"]["enterprise_route_prefixes"]))
for _o in _api_routes.get("community",[]):
    _p=_o.split(" ",1)[1]
    if any(_p==_pf or _p.startswith(_pf+"/") for _pf in _cp_prefixes): bad.append(("admin Community route list has an Enterprise route",_o))
if "enterprise" in _api_routes and set(_api_routes.get("community",[]))-set(_api_routes.get("enterprise",[])): bad.append(("admin Community route list has routes the Enterprise list has not",sorted(set(_api_routes["community"])-set(_api_routes["enterprise"]))[:5]))
if "enterprise" in _api_routes and _api_routes.get("community")==_api_routes.get("enterprise"): bad.append(("admin route lists of the two editions are the same list",))
print(f"admin api contract routes: community {len(_api_routes.get('community',[]))}, enterprise {len(_api_routes.get('enterprise',[]))}")
feats=set(e["enterprise_features"])
# Every file of an Enterprise feature is under the
# extension's features root, in the feature's directory
# (extension.feature_dirs), at the path it had under src/, and nothing of an
# Enterprise feature is under src/. The lists of directories and files that
# were checked here one by one are those directories now: the directories
# under the root are the named ones, and a file there has no twin at the same
# path under src/ (a copy left behind, or a file added back).
FROOT=AX["features_root"]
_fdir=lambda f: AX["feature_dirs"].get(f)
_feature_dirs=set(AX["feature_dirs"].values())
if not FROOT.startswith(AX["root"]+"/"): bad.append(("admin features root outside the extension root",FROOT))
for f in AX["feature_dirs"]:
    if f not in feats: bad.append(("feature_dirs names an unknown feature",f))
_froot_absent=absent(FROOT)
_on_disk={d for d in os.listdir(f"{ADM}/{FROOT}") if not d.startswith(".")} if not _froot_absent and os.path.isdir(f"{ADM}/{FROOT}") else set()
for d in sorted(_on_disk-_feature_dirs): bad.append(("directory under the features root is not a feature's",d))
for d in sorted(_feature_dirs-_on_disk if not _froot_absent else []): bad.append(("feature directory not found under the features root",d))
_feature_files=0
for d in sorted(_on_disk&_feature_dirs):
    for _dp,_dn,_fn in os.walk(f"{ADM}/{FROOT}/{d}"):
        for _f in _fn:
            if _f.startswith("."): continue
            _feature_files+=1
            _under=os.path.relpath(os.path.join(_dp,_f),f"{ADM}/{FROOT}/{d}")
            if os.path.exists(f"{ADM}/src/{_under}"): bad.append(("file of an Enterprise feature has a twin under src/",d,_under))
if not _froot_absent: print(f"admin enterprise feature directories: {len(_on_disk&_feature_dirs)}, files: {_feature_files}")
def _in_feature_dir(q,feature=None):
    """True when a path is in a feature's directory under the features root (that of `feature` when given)."""
    if not q.startswith(FROOT+"/"): return False
    d=q[len(FROOT)+1:].split("/")[0]
    return d==_fdir(feature) if feature else d in _feature_dirs
for slot,items in ac["slots"].items():
    for it in items:
        if it["feature"] not in feats: bad.append(("slot feature",it["feature"]))
# The slot items in code are the rows of `slots` that carry an id: same slot,
# same id, same feature, nothing more and nothing less. And every component
# file an item is made of is a listed Enterprise file. The
# items are the Enterprise extension's (admin_console.extension.slots); the
# console's own registry, src/app/builtinSlots.tsx, holds none.
import re as _re
_own=open(f"{ADM}/src/app/builtinSlots.tsx").read()
if _re.search(r"\bid: '",_own) or "edition:" in _own or _re.search(r"from '\.\./components/",_own): bad.append(("src/app/builtinSlots.tsx holds a slot item or imports a component",))
if not absent(AX["slots"]):
    BS=f"{ADM}/{AX['slots']}"
    bsrc=open(BS).read()
    built=set(); cur=None; cur_id=None
    for line in bsrc.split("\n"):
        m=_re.match(r"^    '([a-z0-9.-]+)': \[$",line)
        if m: cur=m.group(1); continue
        m=_re.match(r"^            id: '([^']+)',$",line)
        if m: cur_id=m.group(1); continue
        m=_re.match(r"^            edition: '([^']+)',$",line)
        if m: built.add((cur,cur_id,m.group(1))); cur_id=None
    declared={(slot,it["id"],it["feature"]) for slot,items in ac["slots"].items() for it in items if "id" in it}
    for row in sorted(built-declared): bad.append(("built-in slot item not in editions.yaml",)+row)
    for row in sorted(declared-built): bad.append(("editions.yaml slot item not built",)+row)
    if bsrc.count("            id: '")!=len(built): bad.append(("a built-in slot item has no edition",bsrc.count("            id: '"),len(built)))
    # An item written on one line would be read by neither pattern above.
    _reg=bsrc[bsrc.index("export const ENTERPRISE_SLOTS"):]
    if len(_re.findall(r"\bid: '",_reg))!=bsrc.count("            id: '"): bad.append(("a built-in slot item is not written one field per line, so it was not checked",))
    slot_names=set(_re.findall(r"^    '([a-z0-9.-]+)': ",open(f"{ADM}/src/app/slots.ts").read(),_re.M))
    if slot_names!={row[0] for row in built}: bad.append(("slots declared in slots.ts differ from the slots with built-in items",sorted(slot_names^{row[0] for row in built})))
    if _re.search(r"from '\.\./",bsrc[:bsrc.index("export const ENTERPRISE_SLOTS")]): bad.append(("the extension's slot file has a relative import the check below does not read",))
    # A component reaches the slot file by a relative path into the features
    # root: every piece a slot item is made of is a file of an Enterprise feature.
    # A component of the Community tree (@community/components/...) is never one.
    _slot_imports=["src/"+q for q in _re.findall(r"from '@community/(components/[^']+)'",bsrc)]
    _slot_imports+=[os.path.normpath(os.path.join(os.path.dirname(AX["slots"]),q)) for q in _re.findall(r"from '(\./[^']+)'",bsrc)]
    for q in _slot_imports:
        cands=[q] if q.endswith(".tsx") else [q+".tsx",q+".ts"]
        hit=[c for c in cands if os.path.isfile(f"{ADM}/{c}")]
        if not hit: bad.append(("built-in slot import not found",q)); continue
        if not _in_feature_dir(hit[0]): bad.append(("built-in slot item file is not a file of an Enterprise feature",hit[0]))
    print(f"admin built-in slot items: {len(built)} in {len({row[0] for row in built})} slots")
# The docs site: not in the public layout.
D=f"{ROOTS['docs']}/src/content/docs" if ROOTS["docs"] else None
if D is None: skip("docs site")
for s in (e["docs"]["enterprise_sections"] if D else []):
    if not os.path.isdir(f"{D}/{s}"): bad.append(("docs section",s))
for p in (e["docs"]["enterprise_pages"]+[c["page"] for c in e["docs"]["enterprise_callouts"]]+e["docs"]["installation_pages_to_rewrite"] if D else []):
    if not (os.path.exists(f"{D}/{p}.mdx") or os.path.exists(f"{D}/{p}.md")): bad.append(("docs page",p))
for c in e["docs"]["enterprise_callouts"]:
    for f in c["features"]:
        if f not in feats: bad.append(("docs feature",f))

# --- Control plane router registry: every router's
# feature tag agrees with enterprise_modules. The Community registry
# (app/composition/routers.py) holds no tagged router and the Enterprise ones
# are the extension's (enterprise/brutor_enterprise/routers.py), all tagged.
# Read from the source with ast, so the app is not imported.
import ast
CPX=f"{CTL}/enterprise/brutor_enterprise"
RP=f"{CTL}/app/composition/routers.py"
# The feature names are constants of app/composition/features.py, which every
# registry imports them from.
ftree=ast.parse(open(f"{CTL}/app/composition/features.py").read())
consts={t.id:n.value.value for n in ftree.body if isinstance(n,ast.Assign) and isinstance(n.value,ast.Constant) for t in n.targets if isinstance(t,ast.Name)}
if set(consts.values())!=set(feats)-{"clustering"}: bad.append(("feature names in features.py differ from editions.yaml",sorted(set(consts.values())^(set(feats)-{"clustering"}))))
def imports(node):
    return {a.asname or a.name:n.module for n in ast.walk(node) if isinstance(n,ast.ImportFrom) for a in n.names}
def endpoint_module(build,top,funcs):
    scope=dict(top); body=build
    if isinstance(build,ast.Name) and build.id in funcs:
        body=funcs[build.id]; scope.update(imports(body))
    for c in ast.walk(body):
        if isinstance(c,ast.Call) and isinstance(c.func,ast.Name):
            m=scope.get(c.func.id,"")
            if m.startswith("app.api.") or (m.startswith("brutor_enterprise.") and ".endpoints." in m): return m
    return None
mod_feature={}
for f,paths in e["control_plane"]["enterprise_modules"].items():
    for q in paths: mod_feature[q]=f
def _cp_rel(module):
    """A control-plane module's path without `.py`: `app.x.y` is app/x/y;
    `brutor_enterprise.x.y` is enterprise/brutor_enterprise/x/y."""
    return ("enterprise/" if module.split(".")[0]=="brutor_enterprise" else "")+module.replace(".","/")
def _cp_trees():
    """The control plane's source trees: Community, and the Enterprise package."""
    return [f"{CTL}/app",f"{CTL}/enterprise/brutor_enterprise"]
def expected(module):
    path=_cp_rel(module)+".py"
    for q,f in mod_feature.items():
        if path==q or (q.endswith("/") and path.startswith(q)): return f
    return None
def _control_registry(path,ctor):
    """(tree, [calls of ctor]) of one control-plane registry file."""
    qtree=ast.parse(open(path).read())
    return qtree,[n for n in ast.walk(qtree) if isinstance(n,ast.Call) and getattr(n.func,"id","")==ctor and n.args and isinstance(n.args[0],ast.Constant)]
def _control_tag(c):
    """(tagged?, feature) of a registry entry: the `feature=` keyword, a known constant."""
    kw={k.arg:k.value for k in c.keywords}
    if "feature" not in kw: return False,None
    return True,consts.get(getattr(kw["feature"],"id",None))
# Nothing of app/composition or app/app.py imports the Enterprise package or an Enterprise module.
for fn in sorted(os.listdir(f"{CTL}/app/composition"))+["../app.py"]:
    if not fn.endswith(".py"): continue
    ctree=ast.parse(open(f"{CTL}/app/composition/{fn}").read())
    # `from a.b import c` may name the module a.b.c; `import a.b` names a.b.
    cmods={n.module for n in ast.walk(ctree) if isinstance(n,ast.ImportFrom) and n.module}|{f"{n.module}.{a.name}" for n in ast.walk(ctree) if isinstance(n,ast.ImportFrom) and n.module for a in n.names}|{a.name for n in ast.walk(ctree) if isinstance(n,ast.Import) for a in n.names}
    for mod in sorted(cmods):
        if mod.split(".")[0]=="brutor_enterprise" or expected(mod): bad.append(("control composition imports the Enterprise Edition",fn,mod))
specs=[]; tagged=0; seen_modules=set(); router_tag={}; router_module={}
for where,path in (("community",RP),("enterprise",f"{CPX}/routers.py")):
    if where=="enterprise" and absent("enterprise/brutor_enterprise/routers.py"): continue
    tree,calls=_control_registry(path,"RouterSpec")
    top={k:v for n in tree.body if isinstance(n,ast.ImportFrom) for k,v in imports(n).items()}
    funcs={n.name:n for n in tree.body if isinstance(n,ast.FunctionDef)}
    for c in calls:
        name=c.args[0].value; specs.append(c)
        has,feat=_control_tag(c)
        if has and feat is None: bad.append(("router feature is not a known constant",name))
        if name in router_tag: bad.append(("router named twice across the two registries",name))
        if where=="community" and has: bad.append(("tagged router in the Community registry",name,feat))
        if where=="enterprise" and not has: bad.append(("untagged router in the Enterprise extension",name))
        module=endpoint_module(c.args[1],top,funcs)
        router_tag[name]=feat; router_module[name]=_cp_rel(module or "")+".py"
        if module is None: bad.append(("router without endpoint module",name)); continue
        seen_modules.add(_cp_rel(module)+".py")
        if feat is not None: tagged+=1
        if feat!=expected(module): bad.append(("router feature",name,feat,expected(module)))
        if feat is not None and feat not in feats: bad.append(("router feature unknown",name,feat))
for q in (mod_feature if ENT else []):
    if q.startswith("app/api/v1/endpoints/") and q.endswith(".py") and q not in seen_modules:
        bad.append(("enterprise endpoint module not in the registry",q))
    # A feature is one package; its router modules are the files of its endpoints/ directory.
    _ed=f"{CTL}/{q}endpoints"
    if q.startswith("enterprise/brutor_enterprise/") and q.endswith("/") and os.path.isdir(_ed):
        for _fn in sorted(os.listdir(_ed)):
            if _fn.endswith(".py") and _fn!="__init__.py" and f"{q}endpoints/{_fn}" not in seen_modules:
                bad.append(("enterprise endpoint module not in the registry",f"{q}endpoints/{_fn}"))
# A split prefix names its two routers: the Community one is untagged, the
# Enterprise one is tagged, and the Community one is built from the Community
# module, which imports nothing of an Enterprise module.
for s in e["control_plane"]["split"]:
    cr,er=s.get("community_router"),s.get("enterprise_router")
    if cr is None and er is None: continue
    if cr not in router_tag: bad.append(("split: community router not in the registry",cr))
    elif router_tag[cr] is not None: bad.append(("split: community router is tagged",cr,router_tag[cr]))
    elif router_module[cr]!=s.get("community_module"): bad.append(("split: community router is built from another module",cr,router_module[cr]))
    if not ENT: pass
    elif er not in router_tag: bad.append(("split: enterprise router not in the registry",er))
    elif router_tag[er] is None: bad.append(("split: enterprise router is not tagged",er))
    cm=s.get("community_module")
    if cm and os.path.exists(f"{CTL}/{cm}"):
        for mod in sorted(set(imports(ast.parse(open(f"{CTL}/{cm}").read())).values())):
            if mod and expected(mod): bad.append(("split: community module imports an enterprise module",cm,mod))
print(f"control routers in the registry: {len(specs)}, tagged enterprise: {tagged}")

import re
# --- Core background task registry: every task's feature tag
# agrees with core_gateway.enterprise_modules, judged by the `services::x` /
# `routes::x` modules the task's function names.
TP=f"{CORE}/src/tasks.rs"
tsrc=open(TP).read()
core_mod={}
for f,paths in e["core_gateway"]["enterprise_modules"].items():
    for q in paths:
        if q.startswith("src/") and " " not in q:
            core_mod[q[4:].rstrip("/").removesuffix(".rs").replace("/","::")]=f
table=re.findall(r'name: "([a-z0-9_]+)",\s*feature: (None|Some\("([a-z_]+)"\)),\s*start: ([a-z0-9_]+),',tsrc)
tagged_tasks=0
for name,_,feat,fn in table:
    feat=feat or None
    if name!=fn: bad.append(("core task name differs from its function",name,fn))
    m=re.search(r"\nfn "+fn+r"\(_?ctx: &TaskContext<'_>\) \{\n(.*?)\n\}\n",tsrc,re.S)
    if not m: bad.append(("core task without a function",name)); continue
    used={mod for mod in re.findall(r"\b((?:services|routes)::[a-z0-9_]+(?:::[a-z0-9_]+)?)",m.group(1))}
    want={f for mod,f in core_mod.items() for u in used if u==mod or u.startswith(mod+"::")}
    if feat: tagged_tasks+=1
    if feat is not None and feat not in e["core_gateway"]["enterprise_modules"]: bad.append(("core task feature unknown",name,feat))
    if want!=({feat} if feat else set()): bad.append(("core task feature",name,feat,sorted(want)))
print(f"core background tasks: {len(table)}, tagged enterprise: {tagged_tasks}")

# --- Core extensions: each feature listed in
# core_gateway.extensions_built is an `Extension` in its module, at interface
# version 1, composed by main.rs; its tasks are listed in the module, tagged
# with the feature, and gone from src/tasks.rs.
# The composition root of the Enterprise distribution: the Community main composes no extension.
core_main=None if absent("enterprise/src/main.rs") else open(f"{CORE}/enterprise/src/main.rs").read()
if ".extension(" in open(f"{CORE}/src/main.rs").read(): bad.append(("core Community main.rs composes an extension",))
if "brutor-license" in open(f"{CORE}/Cargo.toml").read() or "git+" in open(f"{CORE}/Cargo.lock").read(): bad.append(("core Community manifest or lock file names the private licence dependency or a git source",))
builtin_task_names={name for name,_,_,_ in table}
ext_tasks=0
for feat,x in ext_built.items():
    if feat not in e["core_gateway"]["enterprise_modules"]: bad.append(("core extension feature unknown",feat)); continue
    xp=f"{CORE}/{x['module']}"
    if absent(x["module"]):
        # The extension is not in this layout; what the Community tree must not hold still is checked.
        if not any(x["module"]==q or x["module"].startswith(q) for q in e["core_gateway"]["enterprise_modules"][feat] if q.startswith("src/") or q.startswith("enterprise/")): bad.append(("core extension module not listed under its feature",feat,x["module"]))
        for name in x.get("tasks",[]):
            if name in builtin_task_names: bad.append(("core extension task still in src/tasks.rs",name))
        continue
    if not os.path.exists(xp): bad.append(("core extension module",feat,x["module"])); continue
    if not any(x["module"]==q or x["module"].startswith(q) for q in e["core_gateway"]["enterprise_modules"][feat] if q.startswith("src/") or q.startswith("enterprise/")): bad.append(("core extension module not listed under its feature",feat,x["module"]))
    xsrc=open(xp).read()
    if f"pub struct {x['type']}" not in xsrc: bad.append(("core extension type not in its module",feat,x["type"]))
    xm=re.search(r"impl Extension for "+x["type"]+r" \{\n(.*?)\n\}\n",xsrc,re.S)
    if not xm: bad.append(("core extension does not implement Extension",feat,x["type"])); continue
    if not re.search(r"fn interface_version\(&self\) -> u32 \{\s*1\s*\}",xm.group(1)): bad.append(("core extension does not state interface version 1 as a literal",feat))
    if f".extension({x['type']}::" not in core_main: bad.append(("core main.rs does not compose the extension",feat,x["type"]))
    xtable=re.findall(r'name: "([a-z0-9_]+)",\s*feature: (None|Some\("([a-z_]+)"\)),\s*start: ([a-z0-9_]+),',xm.group(1))
    if [n for n,_,_,_ in xtable]!=list(x.get("tasks",[])): bad.append(("core extension tasks differ from extensions_built",feat,[n for n,_,_,_ in xtable]))
    for name,_,tf,fn in xtable:
        ext_tasks+=1
        if tf!=feat: bad.append(("core extension task not tagged with its feature",feat,name,tf or None))
        if name!=fn: bad.append(("core extension task name differs from its function",name,fn))
        if not re.search(r"\nfn "+fn+r"\(_?ctx: &TaskContext<'_>\) \{\n",xsrc): bad.append(("core extension task without a function",name))
        if name in builtin_task_names: bad.append(("core extension task still in src/tasks.rs",name))
print(f"core extensions built: {len(ext_built)}, their background tasks: {ext_tasks}")

# --- Core clustering: Community is one instance. The lease
# is one definition in its module; no Community source names `replica_id`
# and only the listed files read BRUTOR_REPLICA_ID; the Community statistics
# reply names no instance and the seam the extension installs through is in
# the listener; exactly one extension allows several instances, and it is
# the one that carries the per-replica reply.
_cl=e["core_gateway"]["clustering"]
_core=f"{CORE}"
def _core_rs(root):
    for d,_,fs in os.walk(f"{_core}/{root}"):
        for fn in sorted(fs):
            if fn.endswith(".rs"): yield os.path.relpath(os.path.join(d,fn),_core)
for k in ("lease_module","statistics_listener","extension","guard"):
    if not absent(_cl[k]) and not os.path.exists(f"{_core}/{_cl[k]}"): bad.append(("core clustering path",k,_cl[k]))
_key_lit='"'+_cl["lease_key"]+'"'
_key_files=[p for p in _core_rs("src") if _key_lit in open(f"{_core}/{p}").read().split("#[cfg(test)]")[0]]
if _key_files!=[_cl["lease_module"]]: bad.append(("core clustering: the lease key is not defined in the lease module only",_key_files))
for p in _core_rs("src"):
    s=open(f"{_core}/{p}").read()
    if "replica_id" in s: bad.append(("core clustering: Community source names replica_id",p))
    if "REPLICA_ID" in s and p not in _cl["community_replica_env_readers"]: bad.append(("core clustering: Community source reads the replica environment variable",p))
if os.path.exists(f"{_core}/{_cl['statistics_listener']}"):
    s=open(f"{_core}/{_cl['statistics_listener']}").read()
    if f"pub struct {_cl['statistics_seam']}" not in s: bad.append(("core clustering: statistics seam not in the listener",_cl["statistics_seam"]))
    if "hostname::" in s: bad.append(("core clustering: the Community listener names an instance",))
if f"cache_stats_responder: system_notify::{_cl['statistics_seam']}::community()" not in core_state: bad.append(("core state does not start with nobody answering the statistics request for a cluster",))
if "instance_lease: None," not in core_state: bad.append(("core state does not start without an instance lease",))
_several=[p for p in _core_rs("enterprise/src") if re.search(r"fn allows_multiple_instances\(&self\) -> bool \{\s*true\s*\}",open(f"{_core}/{p}").read())] if not absent("enterprise/src") else None
if _several is not None and _several!=[_cl["extension"]]: bad.append(("core clustering: the extensions that allow several instances are not the clustering extension alone",_several))
if [p for p in _core_rs("src") if re.search(r"fn allows_multiple_instances\(&self\) -> bool \{\s*true\s*\}",open(f"{_core}/{p}").read())]: bad.append(("core clustering: a Community source allows several instances",))
if not absent(_cl["extension"]) and os.path.exists(f"{_core}/{_cl['extension']}"):
    s=open(f"{_core}/{_cl['extension']}").read()
    if "pub replica_id: String," not in s or ".cache_stats_responder" not in s: bad.append(("core clustering: the extension does not carry the per-replica reply",))
if _cl["extension"] not in e["core_gateway"]["enterprise_modules"].get("clustering",[]): bad.append(("core clustering: extension not listed under enterprise_modules.clustering",))
if ext_built.get("clustering",{}).get("module")!=_cl["extension"]: bad.append(("core clustering: extension not in extensions_built",))
_bld=open(f"{_core}/src/builder.rs").read().split("#[cfg(test)]")[0]
if not (0 <= _bld.find("connect_redis(config.redis_url") < _bld.find("InstanceLease::take(") < _bld.find("AppState::new(")): bad.append(("core clustering: the builder does not take the lease after Redis and before the state",))
if ".any(|e| e.allows_multiple_instances())" not in _bld: bad.append(("core clustering: the builder does not ask the extensions whether several instances are allowed",))
print(f"core clustering: lease key in {_key_files}, extensions that allow several instances: {_several if _several is not None else 'not in this layout'}")

# --- Core invalidation handler registry: the handlers tagged in
# the code are the ones listed under enterprise_invalidation_handlers; a
# Community handler, the broad eviction and the reconnect flush touch no
# Enterprise field of the invalidator and name no Enterprise module.
IP=f"{CORE}/src/services/cache/invalidation.rs"
isrc=open(IP).read()
itable=re.findall(r'InvalidationHandler \{\s*name: "([a-z0-9_]+)",\s*resource_types: &\[([^\]]*)\],\s*feature: (None|Some\("([a-z_]+)"\)),\s*handle: ([a-z0-9_]+),\s*flush: (None|Some\(([a-z0-9_]+)\)),\s*\}',isrc[isrc.index("pub const BUILTIN_INVALIDATION_HANDLERS"):])
ent_fields=e["core_gateway"]["enterprise_invalidator_fields"]
def rust_fn(src,name):
    m=re.search(r"\n(?:pub\(super\) )?fn "+name+r"<'a>\([^)]*\) -> HandlerFuture<'a> \{\n(.*?)\n\}\n",src,re.S)
    return m.group(1) if m else None
def enterprise_reach(body,owner):
    used={ent_fields[f] for f in ent_fields if re.search(r"\b"+owner+r"\."+f+r"\b",body)}
    mods={mod for mod in re.findall(r"\b((?:services|routes)::[a-z0-9_]+(?:::[a-z0-9_]+)?)",body)}
    return used|{f for mod,f in core_mod.items() for u in mods if u==mod or u.startswith(mod+"::")}
tagged_handlers={}
for name,_,_,feat,fn,_,flush in itable:
    feat=feat or None
    if name!=fn: bad.append(("core invalidation handler name differs from its function",name,fn))
    body=rust_fn(isrc,fn)
    if body is None: bad.append(("core invalidation handler without a function",name)); continue
    if flush:
        fb=rust_fn(isrc,flush)
        if fb is None: bad.append(("core invalidation flush without a function",name,flush)); continue
        body+="\n"+fb
    if feat is not None:
        tagged_handlers[name]=feat
        if feat not in feats: bad.append(("core invalidation handler feature unknown",name,feat))
    elif enterprise_reach(body,"inv"): bad.append(("Community invalidation handler reaches an Enterprise feature",name,sorted(enterprise_reach(body,"inv"))))
if not itable: bad.append(("no core invalidation handlers found",))
# A feature that is an extension registers its own handlers.
# They are listed under extensions_built.<feature>.invalidation_handlers, are
# declared in the extension's module in the same shape, are tagged with the
# feature, and are no longer among the built-in ones.
builtin_handler_names={name for name,_,_,_,_,_,_ in itable}
ext_handler_rows=[]
for feat,x in ext_built.items():
    listed=list(x.get("invalidation_handlers",[]))
    xp=f"{CORE}/{x['module']}"
    if absent(x["module"]) or not os.path.exists(xp): continue
    xsrc=open(xp).read()
    xrows=re.findall(r'InvalidationHandler \{\s*name: "([a-z0-9_]+)",\s*resource_types: &\[([^\]]*)\],\s*feature: (None|Some\("([a-z_]+)"\)),\s*handle: ([a-z0-9_]+),\s*flush: (None|Some\(([a-z0-9_]+)\)),\s*\}',xsrc.split("#[cfg(test)]\nmod tests")[0])
    if [r[0] for r in xrows]!=listed: bad.append(("core extension invalidation handlers differ from extensions_built",feat,[r[0] for r in xrows]))
    if listed and "fn invalidation_handlers(&self)" not in xsrc: bad.append(("core extension lists invalidation handlers and does not contribute them",feat))
    for row in xrows:
        name,_,_,hf,fn,_,flush=row
        if hf!=feat: bad.append(("core extension invalidation handler not tagged with its feature",feat,name,hf or None))
        if name!=fn: bad.append(("core invalidation handler name differs from its function",name,fn))
        if rust_fn(xsrc,fn) is None: bad.append(("core invalidation handler without a function",name))
        if flush and rust_fn(xsrc,flush) is None: bad.append(("core invalidation flush without a function",name,flush))
        if name in builtin_handler_names: bad.append(("core extension invalidation handler still among the built-in ones",name))
        tagged_handlers[name]=hf
        ext_handler_rows.append(row)
_want_handlers=dict(e["core_gateway"]["enterprise_invalidation_handlers"])
if not ENT:
    # Without the Enterprise tree: the built-in handlers tagged in the code are the listed ones no extension registers.
    _want_handlers={k:v for k,v in _want_handlers.items() if k not in {h for x in ext_built.values() for h in x.get("invalidation_handlers",[])}}
if tagged_handlers!=_want_handlers:
    bad.append(("invalidation handlers tagged in the code differ from enterprise_invalidation_handlers",sorted(set(tagged_handlers.items())^set(_want_handlers.items()))))
fb=rust_fn(isrc,"unregistered_type")
if fb is None: bad.append(("core invalidation: no unregistered_type function",))
elif enterprise_reach(fb,"inv"): bad.append(("the broad eviction reaches an Enterprise feature",sorted(enterprise_reach(fb,"inv"))))
msrc=open(f"{CORE}/src/services/cache/mod.rs").read()
msrc=msrc[msrc.index("impl CacheInvalidator {"):msrc.index("#[cfg(test)]\nmod tests")]
if enterprise_reach(msrc,"self"): bad.append(("the invalidation listener itself reaches an Enterprise feature",sorted(enterprise_reach(msrc,"self"))))
# Every type the control plane publishes has a handler, or is listed as known
# to have none. Read from the call sites of the three publishing functions.
registered_types=set()
for _,types,_,_,_,_,_ in itable+ext_handler_rows: registered_types|=set(re.findall(r'"([a-z_]+)"',types))
publishers={"invalidate_proxy_cache","publish_invalidation","publish_invalidation_sync"}
published=set(); unresolved=[]; str_consts={}; calls=[]
for root,_,files in (x for _t in _cp_trees() for x in os.walk(_t)):
    for fn in files:
        if not fn.endswith(".py"): continue
        full=os.path.join(root,fn); mtree=ast.parse(open(full).read())
        for n in mtree.body:
            if isinstance(n,ast.Assign) and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str):
                for t in n.targets:
                    if isinstance(t,ast.Name): str_consts.setdefault(t.id,set()).add(n.value.value)
        for n in ast.walk(mtree):
            if isinstance(n,ast.Call):
                f=n.func; name=f.id if isinstance(f,ast.Name) else (f.attr if isinstance(f,ast.Attribute) else None)
                if name in publishers:
                    v=next((k.value for k in n.keywords if k.arg=="resource_type"),n.args[1] if len(n.args)>1 else None)
                    calls.append((os.path.relpath(full,f"{CTL}"),n.lineno,v))
for rel,line,v in calls:
    if isinstance(v,ast.Constant) and isinstance(v.value,str): published.add(v.value)
    elif isinstance(v,ast.Name) and len(str_consts.get(v.id,()))==1: published|=str_consts[v.id]
    elif rel=="app/services/cache_invalidation.py": pass  # the wrappers passing their own parameter on
    else: unresolved.append((rel,line))
if unresolved: bad.append(("published invalidation type that cannot be read from the source",unresolved))
known_unhandled=set(e["core_gateway"]["published_without_handler"])
if not ENT:
    # Without the extensions' handlers: what the Community tree publishes has a built-in handler or is listed as having none.
    if (published-registered_types)-known_unhandled: bad.append(("published invalidation types without a handler are not listed in published_without_handler",sorted((published-registered_types)-known_unhandled)))
elif published-registered_types!=known_unhandled:
    bad.append(("published invalidation types without a handler differ from published_without_handler",sorted((published-registered_types)^known_unhandled)))
print(f"core invalidation handlers: {len(itable)} built in and {len(ext_handler_rows)} registered by extensions, tagged enterprise: {len(tagged_handlers)}; types published by the control plane: {len(published)}, without a handler: {len(published-registered_types)}")

# --- Control plane startup registry: every step's
# feature tag agrees with enterprise_modules, judged by the app.services
# modules the step imports. The Community list (app/composition/startup.py)
# holds no tagged step; the Enterprise steps are the extension's
# (enterprise/brutor_enterprise/lifecycle.py), all tagged.
def module_feature(module):
    path=_cp_rel(module)
    for q,f in mod_feature.items():
        base=q[:-3] if q.endswith(".py") else q.rstrip("/")
        if path==base or path.startswith(base+"/"): return f
    return None
steps=[]; tagged_steps=0
for where,path in (("community",f"{CTL}/app/composition/startup.py"),("enterprise",f"{CPX}/lifecycle.py")):
    if where=="enterprise" and absent("enterprise/brutor_enterprise/lifecycle.py"): continue
    stree,calls=_control_registry(path,"LifecycleStep")
    sfuncs={n.name:n for n in stree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    for c in calls:
        name=c.args[0].value; steps.append(c)
        has,feat=_control_tag(c)
        if has and feat is None: bad.append(("startup step feature is not a known constant",name))
        if where=="community" and has: bad.append(("tagged startup step in the Community registry",name,feat))
        if where=="enterprise" and not has: bad.append(("untagged startup step in the Enterprise extension",name))
        fn=sfuncs.get(getattr(c.args[1],"id",None))
        if fn is None: bad.append(("startup step without a function",name)); continue
        want={module_feature(n.module) for n in ast.walk(fn) if isinstance(n,ast.ImportFrom) and n.module and n.module.startswith(("app.services","brutor_enterprise."))}-{None}
        if feat is not None: tagged_steps+=1
        if feat is not None and feat not in feats: bad.append(("startup step feature unknown",name,feat))
        if want!=({feat} if feat else set()): bad.append(("startup step feature",name,feat,sorted(want)))
print(f"control startup and shutdown steps: {len(steps)}, tagged enterprise: {tagged_steps}")

# --- Control plane seed registries: the steps the
# Enterprise extension registers (enterprise/brutor_enterprise/seeding.py and
# provisioning.py) are the steps listed under enterprise_seed_steps, with the
# same feature; the Community lists (app/composition) hold no tagged step; and
# a step that imports an Enterprise module carries that module's feature.
tagged_seed=0; total_seed=0
for key,fname,ctor in (("install","seeding.py","SeedStep"),("tenant_provisioning","provisioning.py","ProvisioningStep")):
    declared_steps=dict(e["control_plane"]["enterprise_seed_steps"][key])
    in_code={}
    for where,path in (("community",f"{CTL}/app/composition/{fname}"),("enterprise",f"{CPX}/{fname}")):
        if where=="enterprise" and absent(f"enterprise/brutor_enterprise/{fname}"): continue
        qtree,calls=_control_registry(path,ctor)
        qfuncs={n.name:n for n in qtree.body if isinstance(n,ast.FunctionDef)}
        for c in calls:
            name=c.args[0].value; total_seed+=1
            has,feat=_control_tag(c)
            if has and feat is None: bad.append(("seed step feature is not a known constant",key,name))
            if where=="community" and has: bad.append(("tagged seed step in the Community registry",key,name,feat))
            if where=="enterprise" and not has: bad.append(("untagged seed step in the Enterprise extension",key,name))
            fn=qfuncs.get(getattr(c.args[1],"id",None))
            if fn is None: bad.append(("seed step without a function",key,name)); continue
            if feat is not None:
                in_code[name]=feat; tagged_seed+=1
                if feat not in feats: bad.append(("seed step feature unknown",key,name,feat))
            want={module_feature(n.module) for n in ast.walk(fn) if isinstance(n,ast.ImportFrom) and n.module and n.module.startswith(("app.","brutor_enterprise."))}-{None}
            if want-({feat} if feat else set()): bad.append(("seed step imports an Enterprise module of another feature",key,name,feat,sorted(want)))
    if ENT and in_code!=declared_steps:
        bad.append(("seed steps tagged in the code differ from enterprise_seed_steps",key,sorted(set(in_code.items())^set(declared_steps.items()))))
print(f"control seed and provisioning steps: {total_seed}, tagged enterprise: {tagged_seed}")

# --- Control plane packaging: two distributions, one entry
# point at the repository root, a Dockerfile per edition, and the published
# image built from the Enterprise one. Every line of control_plane.packaging
# is held against the files. (The dependency lists are held against the
# imports by the control plane's own packaging tests.)
import tomllib
_CPR=f"{CTL}"
pk=e["control_plane"]["packaging"]
def _req_name(req): return re.sub(r"[-_.]+","-",re.split(r"[\s\[<>=!~;]",req.strip(),maxsplit=1)[0]).lower()
def _dockerfile_cmd(path):
    lines=[l.strip() for l in open(path).read().splitlines() if l.strip() and not l.strip().startswith("#")]
    return lines[-1] if lines else ""
_deps={}
for ed in ("community","enterprise"):
    row=pk[ed]
    if any(absent(row[k]) for k in ("project","entry_point","dockerfile","dockerignore")): continue
    for key in ("project","entry_point","dockerfile","dockerignore"):
        if not os.path.exists(f"{_CPR}/{row[key]}"): bad.append(("control packaging: file missing",ed,key,row[key]))
    if not os.path.exists(f"{_CPR}/{row['project']}"): continue
    proj=tomllib.load(open(f"{_CPR}/{row['project']}","rb"))
    if proj["project"]["name"]!=row["distribution"]: bad.append(("control packaging: distribution name",ed,proj["project"]["name"]))
    _deps[ed]={_req_name(d) for d in proj["project"]["dependencies"]}
    if proj.get("tool",{}).get("setuptools",{}).get("packages",{}).get("find",{}).get("include")!=[row["python_package"]+"*"]:
        bad.append(("control packaging: the distribution does not ship exactly its package",ed,row["python_package"]))
    sname,starget=[x.strip() for x in row["console_script"].split("=",1)]
    if proj["project"].get("scripts",{})!={sname:starget}: bad.append(("control packaging: console script",ed,proj["project"].get("scripts")))
    smod,sfn=starget.split(":")
    spath=f"{_CPR}/"+("enterprise/" if ed=="enterprise" else "")+smod.replace(".","/")+".py"
    if not os.path.exists(spath) or not re.search(rf"^def {sfn}\(",open(spath).read(),re.M): bad.append(("control packaging: console script names a function that does not exist",ed,starget))
    if os.path.exists(f"{_CPR}/{row['dockerfile']}") and f'"{row["factory"]}"' not in _dockerfile_cmd(f"{_CPR}/{row['dockerfile']}"):
        bad.append(("control packaging: the image does not run its edition's factory",ed,_dockerfile_cmd(f"{_CPR}/{row['dockerfile']}")))
_eo={_req_name(d) for d in pk["enterprise_only_distributions"]}
if _deps.get("community",set())&_eo: bad.append(("control packaging: Enterprise-only library is a Community dependency",sorted(_deps["community"]&_eo)))
if "enterprise" in _deps:
    if _eo-_deps["enterprise"]: bad.append(("control packaging: Enterprise-only library not declared by the Enterprise package",sorted(_eo-_deps["enterprise"])))
    if pk["community"]["distribution"] not in _deps["enterprise"]: bad.append(("control packaging: the Enterprise package does not depend on the Community one",))
    _cv=tomllib.load(open(f"{_CPR}/{pk['community']['project']}","rb"))["project"]["version"]
    _ep=tomllib.load(open(f"{_CPR}/{pk['enterprise']['project']}","rb"))["project"]
    if _ep["version"]!=_cv or f"{pk['community']['distribution']}=={_cv}" not in _ep["dependencies"]: bad.append(("control packaging: the two packages are not at one version",_cv,_ep["version"]))
for p in pk["removed_files"]:
    if os.path.exists(f"{_CPR}/{p}"): bad.append(("control packaging: removed file is back",p))
_root_entries=sorted(f for f in os.listdir(_CPR) if re.fullmatch(r"main.*\.py",f))
if _root_entries!=[pk["community"]["entry_point"]]: bad.append(("control packaging: entry points at the repository root",_root_entries))
if "brutor_enterprise" in re.sub(r'"""[\s\S]*?"""',"",open(f"{_CPR}/{pk['community']['entry_point']}").read()): bad.append(("control packaging: the Community entry point names the Enterprise package",))
_ign=[l.strip() for l in open(f"{_CPR}/{pk['community']['dockerignore']}").read().splitlines()]
if "enterprise/" not in _ign: bad.append(("control packaging: the Community build context contains enterprise/",))
if os.path.exists(f"{_CPR}/{pk['enterprise']['dockerignore']}") and "enterprise/" in [l.strip() for l in open(f"{_CPR}/{pk['enterprise']['dockerignore']}").read().splitlines()]:
    bad.append(("control packaging: the Enterprise build context leaves enterprise/ out",))
_cdf=open(f"{_CPR}/{pk['community']['dockerfile']}").read()
_cdf_code="\n".join(l for l in _cdf.splitlines() if not l.strip().startswith("#"))
for word in ("enterprise","compileall","CLOUD_CLIS"):
    if word in _cdf_code: bad.append(("control packaging: the Community Dockerfile mentions",word))
if ENT:
    # The repository's own image workflow publishes the Enterprise image (private layout only:
    # the public repository's workflows are at its root and publish Community images only).
    _wf=yaml.safe_load(open(f"{_CPR}/{pk['publish_workflow']}"))
    _pushed=[st["with"]["file"].lstrip("./") for job in _wf["jobs"].values() for st in job.get("steps",[]) if isinstance(st.get("with"),dict) and "file" in st["with"] and "push" in st["with"]]
    if _pushed!=[pk["published_image_dockerfile"]]: bad.append(("control packaging: the published image is not built from the Enterprise Dockerfile",_pushed))
    if pk["smoke_script"] not in open(f"{_CPR}/{pk['publish_workflow']}").read() or not os.access(f"{_CPR}/{pk['smoke_script']}",os.X_OK):
        bad.append(("control packaging: the image smoke script is missing, not executable, or not run by the workflow",pk["smoke_script"]))
else:
    skip("control packaging: the Enterprise image workflow")
    if not os.access(f"{_CPR}/{pk['smoke_script']}",os.X_OK): bad.append(("control packaging: the image smoke script is missing or not executable",pk["smoke_script"]))
print(f"control packaging: {len(_deps.get('community',()))} Community and {len(_deps.get('enterprise',()))} Enterprise dependencies, {len(_eo)} Enterprise-only")

# --- Control plane permission registry: the
# Enterprise permissions are an enum of the extension
# (enterprise/brutor_enterprise/permissions.py, `EnterprisePermission`) and are
# the ones listed under enterprise_permissions, with the same feature and the
# same default-role grants; none of them is a member of the Community enum; an
# Enterprise permission is named only by modules of its feature; and a
# Community permission named only by modules of one feature is reported.
import os
CP=f"{CTL}"
def _enum_members(path,cls_name):
    qtree=ast.parse(open(path).read())
    cls=[n for n in qtree.body if isinstance(n,ast.ClassDef) and n.name==cls_name]
    if not cls: bad.append(("permission enum not found",os.path.relpath(path,CP),cls_name)); return qtree,{}
    return qtree,{t.id:n.value.value for n in cls[0].body if isinstance(n,ast.Assign) and isinstance(n.value,ast.Constant) for t in n.targets}
_,community_perm_value=_enum_members(f"{CP}/app/core/admin_user/permissions.py","Permission")
if "ENTERPRISE_PERMISSIONS" in open(f"{CP}/app/composition/permissions.py").read(): bad.append(("the Community permission registry still tags Enterprise permissions",))
if not absent("enterprise/brutor_enterprise/permissions.py"):
    ptree,enterprise_perm_value=_enum_members(f"{CPX}/permissions.py","EnterprisePermission")
    both=sorted((set(community_perm_value)&set(enterprise_perm_value))|{v for v in community_perm_value.values() if v in set(enterprise_perm_value.values())})
    if both: bad.append(("permission in both the Community and the Enterprise enum",both))
    perm_value={**community_perm_value,**enterprise_perm_value}
    pconst={}
    for n in ptree.body:
        tgt=n.targets[0] if isinstance(n,ast.Assign) else (n.target if isinstance(n,ast.AnnAssign) else None)
        if isinstance(tgt,ast.Name) and n.value is not None: pconst[tgt.id]=n.value
    role_names={"_NO_ROLE":[],"_READ_ONLY":["Tenant Read-Only"]}
    table=pconst.get("ENTERPRISE_PERMISSIONS")
    perm_feature={}; perm_roles={}
    if not isinstance(table,ast.Dict): bad.append(("ENTERPRISE_PERMISSIONS is not a dict literal",))
    else:
        for fk,fv in zip(table.keys,table.values):
            feat=consts.get(getattr(fk,"id",None))
            if feat is None or feat not in feats: bad.append(("permission feature is not a known constant",ast.unparse(fk))); continue
            for pk,pv in zip(fv.keys,fv.values):
                name=pk.attr if isinstance(pk,ast.Attribute) and getattr(pk.value,"id","")=="EnterprisePermission" else None
                if name not in enterprise_perm_value: bad.append(("permission is not a member of the Enterprise enum",ast.unparse(pk))); continue
                if name in perm_feature: bad.append(("permission tagged twice",name))
                if getattr(pv,"id",None) not in role_names: bad.append(("permission grant is not a known constant",name,ast.unparse(pv))); continue
                perm_feature[name]=feat; perm_roles[name]=role_names[pv.id]
    untagged=sorted(set(enterprise_perm_value)-set(perm_feature))
    if untagged: bad.append(("Enterprise permission without a feature",untagged))
    in_code={}
    for name,feat in perm_feature.items(): in_code.setdefault(feat,{})[perm_value[name]]=perm_roles[name]
    declared_perms={f:{v:list(r) for v,r in (d or {}).items()} for f,d in e["control_plane"]["enterprise_permissions"].items()}
    if in_code!=declared_perms:
        diff=sorted({(f,v) for f,d in in_code.items() for v in d if declared_perms.get(f,{}).get(v)!=d[v]}|{(f,v) for f,d in declared_perms.items() for v in d if in_code.get(f,{}).get(v)!=d[v]})
        bad.append(("permissions tagged in the code differ from enterprise_permissions",diff))
    named={}
    for root,_,files in (x for _t in _cp_trees() for x in os.walk(_t)):
        for fn in files:
            if not fn.endswith(".py"): continue
            rel=os.path.relpath(os.path.join(root,fn),CP)
            if rel in ("app/core/admin_user/permissions.py","app/composition/permissions.py"): continue
            # The extension's own modules (not under a feature package) define and register the Enterprise permissions.
            if rel.startswith("enterprise/") and module_feature(rel[:-3].replace("/",".")) is None: continue
            text=open(os.path.join(root,fn)).read()
            # `Permission.X` is the Community enum, `EnterprisePermission.X` the extension's.
            for m in set(re.findall(r"(?<![A-Za-z0-9_])Permission\.([A-Z][A-Z0-9_]*)\b",text)):
                if m in community_perm_value: named.setdefault(m,set()).add(rel)
                elif m in enterprise_perm_value: bad.append(("Enterprise permission named through the Community enum",m,rel))
            for m in set(re.findall(r"(?<![A-Za-z0-9_])EnterprisePermission\.([A-Z][A-Z0-9_]*)\b",text)):
                if m in enterprise_perm_value: named.setdefault(m,set()).add(rel)
                else: bad.append(("not a member of the Enterprise permission enum",m,rel))
    for name in perm_value:
        where={rel:module_feature(rel[:-3].replace("/",".")) for rel in named.get(name,())}
        tag=perm_feature.get(name)
        if tag is not None:
            outside=sorted(rel for rel,f in where.items() if f!=tag)
            if outside: bad.append(("enterprise permission named outside its feature",perm_value[name],tag,outside))
        elif where and len(set(where.values()))==1 and None not in where.values():
            bad.append(("permission named only by one Enterprise feature is not tagged",perm_value[name],sorted(set(where.values()))))
    print(f"control permissions: {len(perm_value)}, tagged enterprise: {len(perm_feature)}, granted to Tenant Read-Only by the registry: {sum(1 for r in perm_roles.values() if r)}")

# --- Admin Console screen registry: every screen's edition tag
# agrees with the nav ids listed above. Read from the source, not executed.
# The registry is two lists: the Community screens in
# src/app/screens.tsx, none of them tagged, and the Enterprise screens in the
# extension (admin_console.extension.screens), every one tagged.
scr=open(f"{ADM}/src/app/screens.tsx").read()
scr=scr[scr.index("export const SCREENS"):scr.index("export const FALLBACK_SCREEN")]
escr=None if absent(AX["screens"]) else open(f"{ADM}/{AX['screens']}").read()
escr=escr[escr.index("export const ENTERPRISE_SCREENS"):] if escr is not None else ""
nav_feature={i:f for f,ids in ac["enterprise_nav_ids"].items() for i in ids}
nav_feature.update({i:"multi_tenant" for i in ac["tenant_console_nav_ids"]})
_split=lambda src: re.split(r"\n    \{\n?(?=\s*(?://[^\n]*\n\s*)*name: ')|\n    \{ (?=name: ')",src)[1:]
com_blocks=_split(scr); ent_blocks=_split(escr)
if len(com_blocks)!=scr.count("name: '") or len(ent_blocks)!=escr.count("name: '"): bad.append(("screen registry not readable",len(com_blocks),scr.count("name: '"),len(ent_blocks),escr.count("name: '")))
for b in com_blocks:
    if re.search(r"\bedition: '",b): bad.append(("tagged screen in the Community registry",re.search(r"name: '([^']+)'",b).group(1)))
for b in ent_blocks:
    if not re.search(r"\bedition: '",b): bad.append(("untagged screen in the Enterprise registry",re.search(r"name: '([^']+)'",b).group(1)))
blocks=com_blocks+ent_blocks
claimed={}; tagged_screens=0
for b in blocks:
    name=re.search(r"name: '([^']+)'",b).group(1)
    m=re.search(r"\bids: \[([^\]]*)\]",b)
    ids=re.findall(r"'([^']+)'",m.group(1)) if m else []
    m=re.search(r"\bedition: '([^']+)'",b)
    edition=m.group(1) if m else None
    for i in ids: claimed[i]=name
    want={nav_feature[i] for i in ids if i in nav_feature}
    if edition is not None:
        tagged_screens+=1
        if edition not in feats: bad.append(("screen edition unknown",name,edition))
        if want!={edition}: bad.append(("screen edition",name,edition,sorted(want)))
    elif want: bad.append(("screen not tagged",name,sorted(want)))
# A listed nav id with no screen must be a parent that only expands.
for i in (nav_feature if ENT else []):
    if i not in claimed and not re.search(r"id: '%s'[^\n]*\n\s*(?://[^\n]*\n\s*)*children: \["%re.escape(i),side) and not re.search(r"id: '%s',[^}\n]*children: \["%re.escape(i),side) and not re.search(r"id: '%s',\s*\n(?:[^\n]*\n){0,12}?\s*children: \["%re.escape(i),side):
        bad.append(("enterprise nav id without a screen",i))
print(f"admin screens in the registry: {len(blocks)}, tagged enterprise: {tagged_screens}")

# --- Admin Console API client modules: the modules tagged in
# src/lib/gatewayApi/modules.ts are those of enterprise_api_modules; a module
# of one feature requests no path under another Enterprise feature's route
# prefixes (control_plane.enterprise_route_prefixes), and a Community module
# requests no path under any of them. Paths are the string literals of the
# module file, which is where every request of the client names its route.
# The registry is two lists: the Community modules in
# modules.ts, none of them tagged, and the Enterprise modules in the extension
# (admin_console.extension.api_modules), every one tagged.
GA=f"{ADM}/src/lib/gatewayApi"
mods_src=open(f"{GA}/modules.ts").read()
mods_src=mods_src[mods_src.index("export const BUILTIN_API_MODULES"):mods_src.index("] as const")]
emods_src=None if absent(AX["api_modules"]) else open(f"{ADM}/{AX['api_modules']}").read()
emods_src=emods_src[emods_src.index("export const ENTERPRISE_API_MODULES"):emods_src.index("] as const")] if emods_src is not None else ""
_spec=r"\{ name: '([A-Za-z0-9]+)', feature: (?:'([a-z_]+)'|null), mixin: ([A-Za-z0-9]+) \}"
com_api=re.findall(_spec,mods_src); ent_api=re.findall(_spec,emods_src)
api_modules=com_api+ent_api
if len(api_modules)!=mods_src.count("mixin:")+emods_src.count("mixin:"): bad.append(("api module spec not readable",mods_src.count("mixin:")+emods_src.count("mixin:")-len(api_modules)))
for n,f,_ in com_api:
    if f: bad.append(("tagged api module in the Community registry",n,f))
for n,f,_ in ent_api:
    if not f: bad.append(("untagged api module in the Enterprise registry",n))
code_tagged={n:f for n,f,_ in api_modules if f}
yaml_tagged={n:f for f,ns in ac["enterprise_api_modules"].items() for n in ns}
if ENT and code_tagged!=yaml_tagged: bad.append(("api modules tagged in code differ from enterprise_api_modules",sorted(set(code_tagged.items())^set(yaml_tagged.items()))))
for f in code_tagged.values():
    if f not in feats: bad.append(("api module feature unknown",f))
# Where a module's file is: a Community one in src/lib/gatewayApi; an
# Enterprise one where the extension's list imports its class from, which is
# its feature's directory under the features root, or src/lib/gatewayApi
# until that feature has moved.
def _registry_files(reg_path,reg_text,entries,subdir):
    """{name: file} for the entries of an Enterprise registry file, from its import lines."""
    out={}
    imports={n.strip():spec for names,spec in re.findall(r"import \{([^}]+)\} from '([^']+)';",reg_text) for n in names.split(",")}
    for name,feature,symbol in entries:
        spec=imports.get(symbol)
        if spec is None: bad.append(("enterprise registry entry is not imported",reg_path,symbol)); continue
        if spec.startswith("@community/"): q="src/"+spec[len("@community/"):]+".ts"
        elif spec.startswith(("./","../")): q=os.path.normpath(os.path.join(os.path.dirname(reg_path),spec))+".ts"
        else: bad.append(("enterprise registry import not readable",reg_path,spec)); continue
        if not os.path.isfile(f"{ADM}/{q}"): bad.append(("enterprise registry import not found",reg_path,q)); continue
        if q!=f"{FROOT}/{_fdir(feature)}/{subdir}/{name}.ts": bad.append(("enterprise module file is not in its feature's directory",name,feature,q))
        out[name]=q
    return out
api_file={n:f"src/lib/gatewayApi/{n}.ts" for n,_,_ in com_api}
if ENT: api_file.update(_registry_files(AX["api_modules"],open(f"{ADM}/{AX['api_modules']}").read(),ent_api,"lib/gatewayApi"))
module_files={f[:-3] for f in os.listdir(GA) if f.endswith(".ts") and f not in ("base.ts","modules.ts")}
_in_ga={n for n,q in api_file.items() if q.startswith("src/lib/gatewayApi/")}
if module_files!=_in_ga: bad.append(("api module files differ from the registry",sorted(module_files^_in_ga)))
api_modules=[m for m in api_modules if m[0] in api_file]
route_prefixes=e["control_plane"]["enterprise_route_prefixes"]
def prefix_feature(path):
    for f,ps in route_prefixes.items():
        for pre in ps:
            if path==pre or path.startswith(pre+"/"): return f
    return None
for name,feature,_ in api_modules:
    src=open(f"{ADM}/{api_file[name]}").read()
    paths=set(re.findall(r"[`'\"](/(?:v1|scim)/[^`'\"$?]*)",src))
    for p in sorted(paths):
        pf=prefix_feature(p)
        if pf and pf!=feature: bad.append(("api module requests another feature's route",name,feature or "community",p,pf))
    # Under a prefix split into a Community and an Enterprise router, a
    # Community module requests the Community operations only (a path built
    # with a tenant id is tenant management).
    if not feature:   # the registry's `feature: null` is read as an empty string
        for sp in e["control_plane"]["split"]:
            allowed={op.split(" ")[1] for op in sp.get("community_operations",[])}
            if not allowed: continue
            for p in sorted(paths):
                if (p==sp["prefix"] or p.startswith(sp["prefix"]+"/")) and p not in allowed:
                    bad.append(("community api module requests an enterprise route of a split prefix",name,p))
print(f"admin api client modules: {len(api_modules)}, tagged enterprise: {len(code_tagged)}")

# --- Admin Console useGateway hook modules: the hooks tagged in
# src/hooks/gateway/modules.ts are those of enterprise_hook_modules; a hook of
# one feature calls API client methods of its own feature or of Community
# modules only, and a Community hook calls Community methods only. A method's
# module is where the rule above puts it.
# There are two facades and two lists: useGateway and
# src/hooks/gateway/modules.ts, the Community hooks, none of them tagged; and
# the Enterprise hook (admin_console.extension.hook) with its list
# (extension.hook_modules), every one tagged. Neither facade calls a hook of
# the other list.
GH=f"{ADM}/src/hooks/gateway"
hmods_src=open(f"{GH}/modules.ts").read()
hmods_src=hmods_src[hmods_src.index("export const GATEWAY_HOOK_MODULES"):hmods_src.index("] as const")]
ehmods_src=None if absent(AX["hook_modules"]) else open(f"{ADM}/{AX['hook_modules']}").read()
ehmods_src=ehmods_src[ehmods_src.index("export const ENTERPRISE_GATEWAY_HOOK_MODULES"):ehmods_src.index("] as const")] if ehmods_src is not None else ""
_hspec=r"\{ name: '([A-Za-z0-9]+)', feature: (?:'([a-z_]+)'|null), hook: ([A-Za-z0-9]+) \}"
com_hooks=re.findall(_hspec,hmods_src); ent_hooks=re.findall(_hspec,ehmods_src)
hook_modules=com_hooks+ent_hooks
if len(hook_modules)!=hmods_src.count("hook:")+ehmods_src.count("hook:"): bad.append(("hook module spec not readable",hmods_src.count("hook:")+ehmods_src.count("hook:")-len(hook_modules)))
for n,f,_ in com_hooks:
    if f: bad.append(("tagged hook module in the Community registry",n,f))
for n,f,_ in ent_hooks:
    if not f: bad.append(("untagged hook module in the Enterprise registry",n))
_cfac=open(f"{ADM}/src/hooks/useGateway.ts").read()
_efac=None if absent(AX["hook"]) else open(f"{ADM}/{AX['hook']}").read()
for n,_,h in ent_hooks:
    if re.search(r"\b%s\b"%h,_cfac): bad.append(("the Community gateway hook names an Enterprise domain hook",h))
    if _efac is not None and not re.search(r"= %s\(core\);"%h,_efac): bad.append(("the Enterprise gateway hook does not call its domain hook",h))
for n,_,h in (com_hooks if _efac is not None else []):
    if re.search(r"\b%s\(core"%h,_efac): bad.append(("the Enterprise gateway hook calls a Community domain hook",h))
hook_tagged={n:f for n,f,_ in hook_modules if f}
yaml_hook_tagged={n:f for f,ns in ac["enterprise_hook_modules"].items() for n in ns}
if ENT and hook_tagged!=yaml_hook_tagged: bad.append(("hook modules tagged in code differ from enterprise_hook_modules",sorted(set(hook_tagged.items())^set(yaml_hook_tagged.items()))))
for f in hook_tagged.values():
    if f not in feats: bad.append(("hook module feature unknown",f))
hook_file={n:f"src/hooks/gateway/{n}.ts" for n,_,_ in com_hooks}
if ENT: hook_file.update(_registry_files(AX["hook_modules"],open(f"{ADM}/{AX['hook_modules']}").read(),ent_hooks,"hooks/gateway"))
hook_files={f[:-3] for f in os.listdir(GH) if f.endswith(".ts") and f not in ("core.ts","modules.ts")}
_in_gh={n for n,q in hook_file.items() if q.startswith("src/hooks/gateway/")}
if hook_files!=_in_gh: bad.append(("hook module files differ from the registry",sorted(hook_files^_in_gh)))
hook_modules=[m for m in hook_modules if m[0] in hook_file]
method_module={}
for name,_,_ in api_modules:
    for m in re.findall(r"^    (?:private |protected )?(?:async )?([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>]*>)?\(",open(f"{ADM}/{api_file[name]}").read(),re.M):
        method_module.setdefault(m,name)
for name,feature,_ in hook_modules:
    src=open(f"{ADM}/{hook_file[name]}").read()
    for call in sorted(set(re.findall(r"gatewayClient\.([A-Za-z_][A-Za-z0-9_]*)\(",src))):
        owner=method_module.get(call)
        if owner is None: bad.append(("hook calls a client method no module declares",name,call)); continue
        of=code_tagged.get(owner)
        if of and of!=feature: bad.append(("hook wraps another feature's client method",name,feature or "community",call,owner,of))
print(f"admin hook modules: {len(hook_modules)}, tagged enterprise: {len(hook_tagged)}")
if not ENT:
    _sk=sorted(set(skipped))
    print(f"not checked in the {LAYOUT} layout (the Enterprise trees and the docs site are not in it): {len(_sk)} paths and checks")
print("NOT FOUND:",bad if bad else "none")
raise SystemExit(1 if bad else 0)
