"""
sh3d_xml.py : generation de fragments XML SH3D (Home.xml) et conversion vers un
.sh3d reel via java/Conv.java.

Extrait de build_home.py (aucun changement de comportement) pour etre reutilise
par les projets qui produisent un .sh3d "hors-ligne" independamment du pipeline
extérieur : build_home.py lui-meme, et les scripts du plan 2D interieur par
batiment (interieur_init.py, fusion_interieur.py).

Le format genere (id/level/x/y/elevation en cm, etc.) est celui attendu par
`HomeXMLHandler` de Sweet Home 3D -- cf. docs/PIPELINE.md.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import sitegeo as cg

JCONV = cg.DATA / "_jconv"     # cache : jar copie + Conv.class (partage entre appelants)
FOOTPRINT_CLEARANCE_CM = 3.0   # marge au-dessus du terrain/sol de reference (evite le clipping)

# Nom de la piece-repere (emprise du batiment) creee par interieur_init.py sur
# chaque niveau d'un interieur/<id>.sh3d -- identifiant stable partage avec
# fusion_interieur.py, qui l'utilise pour EXCLURE cette piece du fichier
# fusionne (jamais un element du resultat final, seulement un calque de
# tracage pendant l'edition). Le libelle documente lui-meme ce sort pour
# l'utilisateur qui ouvre le fichier dans l'appli native.
GUIDE_ROOM_NAME = "Repere emprise (auto-exclu de la fusion)"


def esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("'", "&apos;"))


def uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def piece(levels: dict, level, name, model, size, x, y, elev, w, d, h, *, catalog=None,
          creator=None, extra="") -> str:
    a = [f"id='{uid('pieceOfFurniture')}'", f"level='{levels[level]}'"]
    if catalog:
        a.append(f"catalogId='{esc(catalog)}'")
    a.append(f"name='{esc(name)}'")
    if creator:
        a.append(f"creator='{esc(creator)}'")
    a += [f"model='{model}'", "icon='ico'",
          f"x='{x:.1f}'", f"y='{y:.1f}'", f"elevation='{elev:.1f}'",
          f"width='{w:.1f}'", f"depth='{d:.1f}'", f"height='{h:.1f}'",
          f"modelSize='{size}'"]
    return f"  <pieceOfFurniture {' '.join(a)}{extra}/>"


def furniture_group(levels: dict, level, name, children: list[str]) -> str:
    """Regroupe des pieceOfFurniture deja generees en un <furnitureGroup>.

    x/y/width/depth/height du groupe sont calcules par SH3D (HomeFurnitureGroup)
    a partir de la boite englobante des enfants ; le level porte par les enfants
    est ignore par HomeXMLHandler, seul celui du groupe compte.
    """
    if not children:
        return ""
    inner = "\n".join(children)
    return (f"  <furnitureGroup id='{uid('furnitureGroup')}' "
            f"level='{levels[level]}' name='{esc(name)}'>\n{inner}\n"
            f"  </furnitureGroup>")


def room(levels: dict, level, name, ring_cm, *, floor_color, floor_visible=True) -> str:
    pts = "\n".join(f"    <point x='{x:.1f}' y='{y:.1f}'/>" for x, y in ring_cm)
    fv = "" if floor_visible else " floorVisible='false'"
    av = " areaVisible='true'" if floor_visible else ""
    return (f"  <room id='{uid('room')}' level='{levels[level]}' name='{esc(name)}'"
            f"{av}{fv} floorColor='{floor_color}' ceilingVisible='false' "
            f"ceilingFlat='true'>\n{pts}\n  </room>")


def wall(levels: dict, level, xstart, ystart, xend, yend, *, thickness, height,
          wall_at_start=None, wall_at_end=None) -> str:
    """<wall> : schema (xStart/yStart/xEnd/yEnd/thickness/height,
    wallAtStart/wallAtEnd pour chainer des murs par id) verifie
    empiriquement par fusion_interieur.py (JDK + SweetHome3D.jar, cf. son
    docstring) -- jamais ecrit par le pipeline avant ce point, les murs
    etaient toujours traces a la main dans l'appli native."""
    wid = uid("wall")
    a = [f"id='{wid}'", f"level='{levels[level]}'",
         f"xStart='{xstart:.1f}'", f"yStart='{ystart:.1f}'",
         f"xEnd='{xend:.1f}'", f"yEnd='{yend:.1f}'",
         f"thickness='{thickness:.1f}'", f"height='{height:.1f}'"]
    if wall_at_start:
        a.append(f"wallAtStart='{wall_at_start}'")
    if wall_at_end:
        a.append(f"wallAtEnd='{wall_at_end}'")
    return wid, f"  <wall {' '.join(a)}/>"


def level(level_id, name, elevation, index) -> str:
    return (f"  <level id='{level_id}' name='{esc(name)}' elevation='{elevation:.1f}' "
            f"floorThickness='12.0' height='30.0' elevationIndex='{index}'/>")


def compass_tag(north_direction_rad: float = 0.0) -> str:
    """<compass> avec long/lat (radians) du centroide du site (pas stocke au depot).
    `north_direction_rad` : decalage du nord geographique -- 0.0 par defaut
    (repere absolu, ou le nord correspond deja a northDirection=0 par
    construction de `sitegeo.to_cm`). Utilise par interieur_init.py pour un
    fichier en repere local tourne : effet cosmetique seulement (orientation
    du soleil dans l'apercu 3D pendant l'edition), sans impact sur la
    geometrie -- sens/convention exact non revalide empiriquement."""
    lon0, lat0, lon1, lat1 = cg.META.bbox_wgs84
    lon = math.radians((lon0 + lon1) / 2.0)
    lat = math.radians((lat0 + lat1) / 2.0)
    return (f"  <compass x='-100.0' y='50.0' diameter='100.0' "
            f"northDirection='{north_direction_rad:.7f}' "
            f"longitude='{lon:.7f}' latitude='{lat:.7f}' timeZone='Europe/Paris'/>")


def set_walk_camera(head: str, x, y, z: float) -> str:
    """Repositionne la camera observateur (visite 3D) : x/y optionnels, z impose."""
    def repl(m):
        tag = m.group(0)
        if x is not None:
            tag = re.sub(r"\bx='[-\d.]+'", f"x='{x:.1f}'", tag)
            tag = re.sub(r"\by='[-\d.]+'", f"y='{y:.1f}'", tag)
        return re.sub(r"\bz='[-\d.]+'", f"z='{z:.1f}'", tag)
    return re.sub(r"<observerCamera attribute='observerCamera'[^>]*/>", repl,
                  head, count=1)


def prepare_java() -> Path:
    """Copie le .jar SH3D et compile Conv.java dans data/_jconv/ (une seule fois,
    cache partage par tous les appelants -- build_home.py, interieur_init.py,
    fusion_interieur.py)."""
    JCONV.mkdir(parents=True, exist_ok=True)
    jar = JCONV / "SweetHome3D.jar"
    if not jar.exists():
        shutil.copy2(cg.find_sweethome3d_jar(), jar)
    cls = JCONV / "com" / "eteks" / "sweethome3d" / "io" / "Conv.class"
    src = cg.JAVA / "Conv.java"
    if not cls.exists() or cls.stat().st_mtime < src.stat().st_mtime:
        r = subprocess.run(["javac", "-cp", str(jar), "-d", str(JCONV), str(src)],
                           check=False, capture_output=True, text=True)
        if not cls.exists():
            print(r.stdout.strip() or r.stderr.strip()[:800])
            raise SystemExit("javac a echoue (JDK sur le PATH, ou erreur de compilation ci-dessus)")
    return jar


def convert_to_sh3d(raw_zip: Path, out_sh3d: Path) -> None:
    """Etape 2 (Java) : raw_zip {Home.xml + modeles} -> out_sh3d reel (Home
    serialise), via java/Conv.java. Leve SystemExit en cas d'echec."""
    jar = prepare_java()
    r = subprocess.run(
        ["java", "-cp", f"{jar}{os.pathsep}{JCONV}",
         "com.eteks.sweethome3d.io.Conv", str(raw_zip), str(out_sh3d)],
        capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip()[:800])
    if r.returncode != 0 or not out_sh3d.exists():
        raise SystemExit("echec de la conversion Java (voir ci-dessus)")
