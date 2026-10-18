"""research_split.py - where the 2020+ research files live after the v5 C-E2 split.

ops/staged_sudo/split_research_dataset.sh moves the 2020+ DERA zips from raw/<dir>/ to
raw/research_zips/<dir>/ (their own never-snapshotted dataset), and encrypt_full_period.sh may
later remove the plaintext, leaving only offbox/full_period_<date>.manifest (sha256 + path of
every archived file).  The download check and the downloader use these helpers so that a moved
or archived file is neither reported missing nor fetched again into raw/<dir>/.
"""
import glob
import os

RESEARCH_ZIPS = os.path.join("raw", "research_zips")


def zip_path(root, d, name):
    """The on-disk path of raw/<d>/<name>: the original place, else the research dataset; None if neither."""
    for p in (os.path.join(root, "raw", d, name), os.path.join(root, RESEARCH_ZIPS, d, name)):
        if os.path.isfile(p) and not os.path.islink(p):
            return p
    return None


def archived(root):
    """{relative path: sha256} from the newest off-box archive manifest whose archive exists, else {}."""
    for m in sorted(glob.glob(os.path.join(root, "offbox", "full_period_*.manifest")), reverse=True):
        if not os.path.isfile(m[:-len(".manifest")] + ".tar.gpg"):
            continue
        out = {}
        with open(m, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if len(line) > 66 and line[64:66] == "  ":
                    out[line[66:]] = line[:64]
        return out
    return {}


def archived_zip(arch, d, name, sha):
    """True if raw/<d>/<name> was moved to the research dataset and archived with this sha256."""
    return arch.get(os.path.join(RESEARCH_ZIPS, d, name)) == sha
