# ovr_viseme_map.py
#
# Mapping from CMU phonemes to your rig's predefined viseme shape keys.
#
from typing import Dict

# Exact viseme shape key names present on the models
OVR_VISEME_KEYS = [
    "viseme_sil",
    "viseme_PP",
    "viseme_FF",
    "viseme_TH",
    "viseme_DD",
    "viseme_kk",
    "viseme_CH",
    "viseme_SS",
    "viseme_nn",
    "viseme_RR",
    "viseme_aa",
    "viseme_E",
    "viseme_I",
    "viseme_O",
]

# CMU -> viseme key mapping (best-effort grouping to your available shapes)
# Notes:
# - Alveolar stops T/D map to "DD"
# - Velars K/G/NG map to "kk"
# - Nasal N maps to "nn"
# - L grouped with alveolar "DD" (common lipsync simplification)
# - SH/ZH grouped with "CH" (rounded fricative/affricate)
# - Y -> "I" (front vowel-ish), W -> "O" (rounded)
# - Vowels split across "aa" (open), "E" (mid/front), "I" (high front), "O" (rounded)
PHONEME_TO_OVR: Dict[str, str] = {
    # silence / breaks
    "SIL": "viseme_sil",
    "SP": "viseme_sil",
    "PAUSE": "viseme_sil",
    # bilabials
    "M": "viseme_PP",
    "B": "viseme_PP",
    "P": "viseme_PP",
    # labiodentals
    "F": "viseme_FF",
    "V": "viseme_FF",
    # dentals
    "TH": "viseme_TH",
    "DH": "viseme_TH",
    # alveolars
    "T": "viseme_DD",
    "D": "viseme_DD",
    "N": "viseme_nn",
    "S": "viseme_SS",
    "Z": "viseme_SS",
    "L": "viseme_DD",
    "R": "viseme_RR",
    # post-alveolars / palato-alveolars
    "SH": "viseme_CH",
    "ZH": "viseme_CH",
    "CH": "viseme_CH",
    "JH": "viseme_CH",
    # velars
    "K": "viseme_kk",
    "G": "viseme_kk",
    "NG": "viseme_kk",
    # glides
    "Y": "viseme_I",
    "W": "viseme_O",
    # vowels
    "AA": "viseme_aa",
    "AE": "viseme_aa",
    "AH": "viseme_aa",
    "AO": "viseme_O",
    "OW": "viseme_O",
    "UH": "viseme_O",
    "UW": "viseme_O",
    "EH": "viseme_E",
    "EY": "viseme_E",
    "IH": "viseme_I",
    "IY": "viseme_I",
    "ER": "viseme_E",  # rhotic vowel – approximate to mid
    "AX": "viseme_E",  # schwa → mid
}

def phoneme_to_viseme(phoneme: str) -> str:
    p = (phoneme or "").strip().upper()
    if not p:
        return "viseme_sil"
    return PHONEME_TO_OVR.get(p, "viseme_E")

def is_viseme_key(name: str) -> bool:
    return name in OVR_VISEME_KEYS


