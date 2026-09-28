"""
interieur_from_survey.py : genere un .sh3d autonome (murs + pieces) a partir
d'un releve de pieces decrit dans un fichier JSON local, git-ignore.

Script generique, sans aucune mesure reelle codee en dur ici (cf. CLAUDE.md,
confidentialite -- un plan interieur reel est aussi sensible que la
geometrie exacte du site). Contrairement a interieur_init.py (qui cree
uniquement une piece-repere reprenant l'emprise BD TOPO exterieure d'un
batiment, jamais de mur), ce script trace de vrais <wall> a partir de
mesures de releve manuel -- premiere utilisation de sh3d_xml.wall() dans le
depot, jusqu'ici les murs etaient toujours dessines a la main dans l'appli
Sweet Home 3D native.

Format attendu du fichier d'entree (voir docstring de `load_survey`) :
liste de pieces, chacune avec ses sommets (polygone en cm, repere local
propre au fichier), une epaisseur de mur par cote, et une hauteur de mur
optionnelle. Aucune ouverture (porte/fenetre) n'est modelisee ici : elles
restent a placer a la main dans l'appli depuis le catalogue (convention du
projet, cf. CLAUDE.md section "Plan 2D interieur"), le script se contente
d'imprimer leurs cotes/positions en fin de generation pour reference.

Usage : `python src/interieur_from_survey.py <releve.json>`
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import sh3d_xml
import sitegeo as cg

DEFAULT_WALL_HEIGHT_CM = 250.0


def load_survey(path: Path) -> dict:
    """Charge le releve JSON. Schema :
    {
      "output": "interieur/<nom>.sh3d",           # chemin de sortie
      "level_name": "Etage",                       # nom du <level> unique
      "rooms": [
        {
          "name": "...",
          "floor_color": "40B0A48F",               # ARGB hex, cf. sh3d_xml.room
          "vertices": [[x, y], ...],               # polygone ferme (dernier -> premier implicite)
          "wall_thickness": [t01, t12, ...],        # une valeur par cote, meme longueur que vertices
          "wall_height": 275.0,                     # optionnel, sinon DEFAULT_WALL_HEIGHT_CM
          "openings": [                             # informatif seulement, jamais modelise
            {"label": "...", "edge": 0, "distance_from_start": 165.5, "width": 82.5}
          ]
        }, ...
      ]
    }
    """
    return json.loads(path.read_text(encoding="utf-8"))


def _room_walls(levels, level_name, room) -> list[str]:
    """Un cote a `wall_thickness` null/0 n'est PAS trace ici -- cas d'un mur
    deja trace par une autre piece du meme releve (mur mitoyen partage,
    cf. cloison Chambre parentale/Palier : trace une seule fois, cote a la
    fois flanc Chambre, pas duplique flanc Palier) : dupliquer produirait
    deux <wall> superposes au meme endroit (rendu double epaisseur / z-fighting)."""
    verts = [tuple(p) for p in room["vertices"]]
    thick = room["wall_thickness"]
    if len(thick) != len(verts):
        raise SystemExit(f"{room['name']!r}: wall_thickness doit avoir {len(verts)} valeurs "
                          f"(une par cote), {len(thick)} fournie(s)")
    height = room.get("wall_height", DEFAULT_WALL_HEIGHT_CM)
    xml = []
    prev_id = None
    n = len(verts)
    for i in range(n):
        if not thick[i]:
            prev_id = None   # cote sans mur ici (mitoyen trace ailleurs) -- casse juste le chainage cosmetique
            continue
        x0, y0 = verts[i]
        x1, y1 = verts[(i + 1) % n]
        wid, tag = sh3d_xml.wall(levels, level_name, x0, y0, x1, y1,
                                  thickness=thick[i], height=height,
                                  wall_at_start=prev_id)
        xml.append(tag)
        prev_id = wid
    return xml


def build(survey: dict) -> tuple[str, list[str]]:
    level_name = survey.get("level_name", "Etage")
    level_id = sh3d_xml.uid("level")
    levels = {level_name: level_id}

    rooms_xml, walls_xml, openings_info = [], [], []
    for room in survey["rooms"]:
        verts = [tuple(p) for p in room["vertices"]]
        rooms_xml.append(sh3d_xml.room(levels, level_name, room["name"], verts,
                                        floor_color=room.get("floor_color", "40B0A48F")))
        walls_xml += _room_walls(levels, level_name, room)
        for op in room.get("openings", []):
            dist = op["distance_from_start"]
            dist_txt = f"a {dist} cm du debut" if dist is not None else "position non mesuree"
            openings_info.append(f"  - {room['name']!r} / {op['label']}: "
                                  f"cote {op['edge']}, {dist_txt}, "
                                  f"largeur {op['width']} cm (a placer a la main dans l'appli)")

    cx = sum(x for r in survey["rooms"] for x, _ in r["vertices"]) / \
        sum(len(r["vertices"]) for r in survey["rooms"])
    cy = sum(y for r in survey["rooms"] for x, y in r["vertices"]) / \
        sum(len(r["vertices"]) for r in survey["rooms"])

    head = (
        "<?xml version='1.0'?>\n"
        f"<home version='7400' name='survey.sh3d' camera='topCamera' wallHeight='"
        f"{DEFAULT_WALL_HEIGHT_CM:.1f}'>\n"
        "  <environment groundColor='FF8A9A5B' skyColor='FFB9D4E8' lightColor='00D0D0D0' "
        "ceillingLightColor='00D0D0D0' photoWidth='400' photoHeight='400' "
        "photoAspectRatio='SQUARE_RATIO' photoQuality='3' videoWidth='320' "
        "videoAspectRatio='RATIO_4_3' videoQuality='0' videoFrameRate='25'/>\n"
        # pas de <compass> : sh3d_xml.compass_tag() lit cg.META (origine
        # Lambert-93 du site exterieur, data/meta.json) -- hors de propos
        # pour ce script generique, non lie au pipeline exterieur georeference.
        f"  <observerCamera attribute='observerCamera' lens='PINHOLE' x='{cx:.1f}' "
        f"y='{cy:.1f}' z='{cg.WALK_EYE_CM:.1f}' yaw='0.0' pitch='0.0' "
        "fieldOfView='1.0995575'/>\n"
        f"  <camera attribute='topCamera' lens='PINHOLE' x='{cx:.1f}' y='{cy:.1f}' "
        "z='2000.0' yaw='0.0' pitch='1.5' fieldOfView='1.0995575'/>\n"
        f"  {sh3d_xml.level(level_id, level_name, 0.0, 0)}\n"
    )
    body = "\n".join(rooms_xml) + "\n" + "\n".join(walls_xml) + "\n</home>\n"
    return head + body, openings_info


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python src/interieur_from_survey.py <releve.json>")
    survey_path = Path(sys.argv[1])
    survey = load_survey(survey_path)
    home_xml, openings_info = build(survey)

    out = cg.ROOT / survey["output"]
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = out.with_name(f"_{out.stem}_raw.zip")
    with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Home.xml", home_xml)
    try:
        sh3d_xml.convert_to_sh3d(raw, out)
    finally:
        raw.unlink(missing_ok=True)

    print(f">>> {out} genere ({len(survey['rooms'])} piece(s)).")
    if openings_info:
        print("Ouvertures a placer a la main dans l'appli (catalogue porte/fenetre) :")
        for line in openings_info:
            print(line)


if __name__ == "__main__":
    main()
