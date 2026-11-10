"""kind = "company-skills": each of the target's `company_skills` must be a copy of the source skill
(content hash). The check shares its sh with install and rollback, so it stays in engine/company_skills.py;
this module only registers it.
"""
import company_skills as source  # the top-level engine/company_skills.py
import remote

KIND = "company-skills"


def validate(exp: dict, target: dict, label: str) -> list:
    if "company_skills" not in target:
        return [(f"{label} 是 company-skills，但落点没有 company_skills 列表",
                 "给落点写 company_skills = [...] 和 company_skills_dir = \"~/...\"")]
    return []


def check_local(manifest: dict, target: dict, exp: dict, rules_text) -> list:
    return source.local_cells(manifest, target, exp)


def check_remote(manifest: dict, spec: dict, env: str, target: dict, exp: dict, rules_text):
    try:
        return source.remote_fragment(manifest, spec, target, exp)
    except source.CompanySkillsError as exc:
        raise remote.RemoteError(str(exc))
