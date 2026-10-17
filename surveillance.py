#!/usr/bin/env python3
"""Surveillance du Nasdaq 100 : repère les mouvements notables et alerte sur Telegram.

Lancé toutes les 5 minutes par launchd sur le Mac, et par GitHub Actions si le relais est configuré
(GITHUB_DEPOT dans .env). Répartition des rôles :
  - Mac : alertes de séance ; signale sa présence à GitHub à chaque passage.
  - GitHub : calendrier des résultats et bilan de clôture, plus les alertes de séance quand le Mac
    ne s'est pas manifesté depuis 15 min (éteint ou en veille).
Sans relais, le Mac fait tout. Ne fait rien hors séance US, sauf le bilan (16h00-16h30 New York,
jusqu'à 17h30 sur GitHub dont les lancements prennent du retard).
Usage : python surveillance.py [--force] [--bilan] [--dry-run]
  --force    analyse même hors séance (sur la dernière séance disponible)
  --bilan    produit le bilan de clôture tout de suite
  --dry-run  affiche les alertes sans les envoyer ni les mémoriser
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, time, timedelta

import pandas as pd

from commun import (ALERTES, DONNEES, INDICE, NY, PARIS, atr_pct, charger_config, charger_env, charger_tickers,
                    donnees_intraday, envoyer_telegram, fondamentaux, historique_journalier, macd,
                    prochains_resultats, rsi)

OUVERTURE, CLOTURE, FIN_BILAN = time(9, 30), time(16, 0), time(16, 30)
FIN_BILAN_GITHUB = time(17, 30)
DUREE_SEANCE_MIN = 390
SILENCE_MAC_MAX = timedelta(minutes=15)
SUR_GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"


def seance_ouverte(maintenant):
    return maintenant.weekday() < 5 and OUVERTURE <= maintenant.time() < CLOTURE


def heure_du_bilan(maintenant):
    fin = FIN_BILAN_GITHUB if SUR_GITHUB else FIN_BILAN
    return maintenant.weekday() < 5 and CLOTURE <= maintenant.time() < fin


def signaler_presence(depot, maintenant):
    """Le Mac note l'heure de son passage dans une variable du dépôt GitHub (via la CLI gh déjà connectée)."""
    gh = shutil.which("gh") or "/opt/homebrew/bin/gh"
    try:
        subprocess.run([gh, "variable", "set", "MAC_VU", "--repo", depot, "--body", maintenant.isoformat()],
                       check=True, capture_output=True, timeout=30)
    except Exception as erreur:
        print(f"{maintenant:%Y-%m-%d %H:%M} signal de présence à GitHub impossible : {erreur}", file=sys.stderr)


def mac_actif(maintenant):
    """Sur GitHub : le Mac s'est-il manifesté récemment ? (variable MAC_VU transmise par le workflow)"""
    vu = os.environ.get("MAC_VU")
    try:
        return bool(vu) and maintenant - datetime.fromisoformat(vu) < SILENCE_MAC_MAX
    except ValueError:
        return False


def jour_ouvre_suivant(jour):
    suivant = jour + timedelta(days=1)
    while suivant.weekday() >= 5:
        suivant += timedelta(days=1)
    return suivant


def alerte_resultats(resultats, jour):
    """Message du jour listant les sociétés qui publient aujourd'hui ou au prochain jour ouvré."""
    lignes = []
    for libelle, date in (("aujourd'hui", jour), ("prochaine séance", jour_ouvre_suivant(jour))):
        societes = [f"{t}" + (f" (BPA attendu {r['bpa_attendu']:.2f} $)" if r.get("bpa_attendu") else "")
                    for t, r in sorted(resultats.items()) if r["date"] == str(date)]
        if societes:
            lignes.append(f"📅 Résultats {libelle} : " + ", ".join(societes))
    return ("*", "resultats", "\n".join(lignes), 0.0) if lignes else None


def alertes_secteurs(forts, industries, seuils, contexte_indice):
    """Regroupe les titres d'une même industrie qui bougent fortement dans le même sens."""
    groupes = {}
    for t, var in forts.items():
        if industries.get(t):
            groupes.setdefault((industries[t], var > 0), []).append(t)
    alertes, membres = [], set()
    for (industrie, hausse), titres in sorted(groupes.items()):
        if len(titres) < seuils["secteur_min_titres"]:
            continue
        titres.sort(key=lambda t: -abs(forts[t]))
        moyenne = sum(forts[t] for t in titres) / len(titres)
        detail = ", ".join(f"{t} {forts[t]:+.1f} %" for t in titres)
        alertes.append(("*", f"secteur_{industrie}_{'hausse' if hausse else 'baisse'}",
                        f"🏭 {industrie} en {'hausse' if hausse else 'baisse'} groupée : {detail} "
                        f"(moyenne {moyenne:+.1f} %{contexte_indice})", 0.0))
        membres.update(titres)
    return alertes, membres


def detecter(tickers, seuils, resultats, industries):
    """Renvoie les alertes (ticker, type, message, prix), l'heure de la dernière bougie, la variation de l'indice
    et la variation du jour de chaque titre."""
    hist = historique_journalier(tickers + [INDICE])
    ouv_i, close_i, vol_i = donnees_intraday(tickers + [INDICE])
    if close_i.empty:
        return [], None, None, {}

    derniere_bougie = close_i.index[-1].tz_convert(NY)
    hist = hist[hist.index.date < derniere_bougie.date()]
    jour, veille_jour = derniere_bougie.date(), hist.index[-1].date()
    debut = derniere_bougie.replace(hour=9, minute=30, second=0, microsecond=0)
    ecoule = (derniere_bougie - debut).total_seconds() / 60 + 5
    fraction = min(max(ecoule, 5) / DUREE_SEANCE_MIN, 1)

    var_indice = None
    if INDICE in close_i.columns and not close_i[INDICE].dropna().empty:
        var_indice = (close_i[INDICE].dropna().iloc[-1] / hist["Close"][INDICE].dropna().iloc[-1] - 1) * 100
    contexte_indice = f", indice {var_indice:+.1f} %" if var_indice is not None else ""

    alertes, variations, forts = [], {}, {}
    for t in tickers:
        if t not in close_i.columns or t not in hist["Close"].columns:
            continue
        serie = close_i[t].dropna()
        clotures = hist["Close"][t].dropna()
        if serie.empty or clotures.empty:
            continue
        variations[t] = (serie.iloc[-1] / clotures.iloc[-1] - 1) * 100
        if len(clotures) < 200:
            continue
        hauts, bas = hist["High"][t].loc[clotures.index], hist["Low"][t].loc[clotures.index]
        prix, veille = serie.iloc[-1], clotures.iloc[-1]
        atr = atr_pct(hauts, bas, clotures)

        date_resultats = (resultats.get(t) or {}).get("date")
        etiquette = {str(jour): " 📅 résultats aujourd'hui", str(veille_jour): " 📅 résultats hier"}.get(date_resultats, "")

        def alerte(type_, message):
            alertes.append((t, type_, message + etiquette, round(float(prix), 2)))

        # Variation de la journée, rapportée à la volatilité habituelle du titre (ATR)
        var_jour = variations[t]
        if abs(var_jour) >= max(seuils["secteur_variation_min_pct"], seuils["secteur_variation_atr"] * atr):
            forts[t] = var_jour
        if abs(var_jour) >= max(seuils["variation_jour_min_pct"], seuils["variation_jour_atr"] * atr):
            sens = "hausse" if var_jour > 0 else "baisse"
            alerte(f"jour_{sens}", f"{'🚀' if var_jour > 0 else '🔻'} {t} {var_jour:+.1f} % sur la journée, "
                                   f"{abs(var_jour) / atr:.1f}× sa volatilité habituelle ({prix:.2f} ${contexte_indice})")

        ouvertures = ouv_i[t].dropna()
        if not ouvertures.empty:
            gap = (ouvertures.iloc[0] / veille - 1) * 100
            if abs(gap) >= max(seuils["gap_min_pct"], seuils["gap_atr"] * atr):
                sens = "haussier" if gap > 0 else "baissier"
                comble = prix <= veille if gap > 0 else prix >= veille
                etat = "déjà comblé" if comble else f"le titre est à {var_jour:+.1f} %"
                alerte(f"gap_{sens}", f"↕️ {t} gap {sens} de {gap:+.1f} % à l'ouverture ({etat})")

        if len(serie) >= 4:
            var_15 = (prix / serie.iloc[-4] - 1) * 100
            if abs(var_15) >= max(seuils["variation_15min_min_pct"], seuils["variation_15min_atr"] * atr):
                sens = "hausse" if var_15 > 0 else "baisse"
                alerte(f"15min_{sens}", f"⚡ {t} {var_15:+.1f} % en 15 min ({prix:.2f} ${contexte_indice})")

        ratio_volume = None
        vol_moyen = hist["Volume"][t].loc[clotures.index].iloc[-20:].mean()
        if ecoule >= 30 and vol_moyen > 0:
            ratio_volume = vol_i[t].sum() / (vol_moyen * fraction)
            if ratio_volume >= seuils["volume_multiple"]:
                alerte("volume", f"📢 {t} volume ×{ratio_volume:.1f} par rapport à la normale ({var_jour:+.1f} %)")

        plus_haut, plus_bas = hauts.iloc[-252:].max(), bas.iloc[-252:].min()
        if prix > plus_haut:
            alerte("plus_haut_52s", f"🏔️ {t} nouveau plus haut sur 52 semaines ({prix:.2f} $, ancien {plus_haut:.2f} $)")
        elif prix < plus_bas:
            alerte("plus_bas_52s", f"🕳️ {t} nouveau plus bas sur 52 semaines ({prix:.2f} $, ancien {plus_bas:.2f} $)")

        avec_jour = pd.concat([clotures, pd.Series([prix])], ignore_index=True)
        mm50_hier, mm200_hier = clotures.iloc[-50:].mean(), clotures.iloc[-200:].mean()
        mm50, mm200 = avec_jour.iloc[-50:].mean(), avec_jour.iloc[-200:].mean()
        if (veille < mm200_hier) != (prix < mm200):
            sens = "au-dessus" if prix > mm200 else "sous"
            icone, libelle = ("📈", "au-dessus de") if prix > mm200 else ("📉", "sous")
            alerte(f"prix_mm200_{sens}", f"{icone} {t} repasse {libelle} sa MM200 ({mm200:.2f} $)")
        if (mm50_hier < mm200_hier) != (mm50 < mm200):
            if mm50 > mm200:
                alerte("golden_cross", f"✨ {t} golden cross : MM50 passe au-dessus de la MM200")
            else:
                alerte("death_cross", f"☠️ {t} death cross : MM50 passe sous la MM200")

        # MACD : croisement avec la ligne de signal, retenu seulement si le volume le confirme
        if ratio_volume is not None and ratio_volume >= seuils["macd_volume_min"]:
            ligne, signal = macd(avec_jour)
            avant, apres = ligne.iloc[-2] - signal.iloc[-2], ligne.iloc[-1] - signal.iloc[-1]
            if (avant < 0) != (apres < 0):
                sens = "hausse" if apres > 0 else "baisse"
                alerte(f"macd_{sens}", f"〽️ {t} MACD croise sa ligne de signal à la {sens} (volume ×{ratio_volume:.1f})")

        valeur_rsi = rsi(avec_jour)
        if valeur_rsi <= seuils["rsi_bas"]:
            alerte("rsi_bas", f"🧊 {t} RSI {valeur_rsi:.0f} (survente)")
        elif valeur_rsi >= seuils["rsi_haut"]:
            alerte("rsi_haut", f"🔥 {t} RSI {valeur_rsi:.0f} (surachat)")

    # Un mouvement de groupe remplace les alertes de variation journalière de ses membres
    groupes, membres = alertes_secteurs(forts, industries, seuils, contexte_indice)
    alertes = [a for a in alertes if not (a[0] in membres and a[1].startswith("jour_"))] + groupes

    message_resultats = alerte_resultats(resultats, jour)
    if message_resultats:
        alertes.append(message_resultats)
    return alertes, derniere_bougie, var_indice, variations


def bilan_cloture(variations, var_indice, fonda, resultats, jour):
    """Résumé de la séance : plus fortes hausses et baisses, largeur du marché, secteurs, résultats à venir."""
    classement = sorted(variations.items(), key=lambda x: x[1])
    hausses = sum(v > 0 for v in variations.values())
    lignes = []
    if var_indice is not None:
        lignes.append(f"Indice (QQQ) {var_indice:+.1f} % | {hausses} titres en hausse, {len(variations) - hausses} en baisse")
    lignes.append("🚀 Plus fortes hausses : " + ", ".join(f"{t} {v:+.1f} %" for t, v in reversed(classement[-5:])))
    lignes.append("🔻 Plus fortes baisses : " + ", ".join(f"{t} {v:+.1f} %" for t, v in classement[:5]))

    secteurs = {}
    for t, v in variations.items():
        secteur = (fonda.get(t) or {}).get("sector")
        if secteur:
            secteurs.setdefault(secteur, []).append(v)
    moyennes = sorted((sum(v) / len(v), s) for s, v in secteurs.items() if len(v) >= 3)
    if len(moyennes) >= 2:
        lignes.append(f"🏭 Secteur le plus fort : {moyennes[-1][1]} {moyennes[-1][0]:+.1f} % | "
                      f"le plus faible : {moyennes[0][1]} {moyennes[0][0]:+.1f} %")

    journal = ALERTES / f"{jour}.jsonl"
    if journal.exists():
        lignes.append(f"🔔 {len(journal.read_text().splitlines())} alerte(s) envoyée(s) pendant la séance")
    prochaine = jour_ouvre_suivant(jour)
    societes = [t for t, r in sorted(resultats.items()) if r["date"] == str(prochaine)]
    if societes:
        lignes.append(f"📅 Résultats prochaine séance : {', '.join(societes)}")
    return ("*", "bilan", "\n".join(lignes), 0.0)


def filtrer_nouvelles(alertes, etat, maintenant, rappel_minutes):
    """Ne garde que les alertes pas encore envoyées aujourd'hui (ou après le délai pour les 15 min)."""
    nouvelles = []
    for alerte in alertes:
        cle = f"{alerte[0]}|{alerte[1]}"
        deja = etat.get(cle)
        if deja is None:
            nouvelles.append(alerte)
        elif alerte[1].startswith("15min") and maintenant - datetime.fromisoformat(deja) >= timedelta(minutes=rappel_minutes):
            nouvelles.append(alerte)
    return nouvelles


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--bilan", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    maintenant = datetime.now(NY)
    depot = None if SUR_GITHUB else charger_env().get("GITHUB_DEPOT")  # relais GitHub configuré
    bilan = args.bilan or (not args.force and not depot and heure_du_bilan(maintenant))
    if not (args.force or bilan or seance_ouverte(maintenant)):
        return
    if depot and not args.dry_run:
        signaler_presence(depot, maintenant)
    if bilan and not args.dry_run:
        fichier_etat = DONNEES / f"etat-{maintenant.date()}.json"
        if fichier_etat.exists() and "*|bilan" in json.loads(fichier_etat.read_text()):
            return

    config = charger_config()
    tickers = charger_tickers()
    try:
        resultats = prochains_resultats(tickers)
    except Exception as erreur:
        print(f"{maintenant:%Y-%m-%d %H:%M} calendrier des résultats indisponible : {erreur}", file=sys.stderr)
        resultats = {}
    try:
        fonda = fondamentaux(tickers)
    except Exception as erreur:
        print(f"{maintenant:%Y-%m-%d %H:%M} données fondamentales indisponibles : {erreur}", file=sys.stderr)
        fonda = {}
    industries = {t: f.get("industry") for t, f in fonda.items()}
    alertes, derniere_bougie, var_indice, variations = detecter(tickers, config["seuils"], resultats, industries)
    if derniere_bougie is None:
        print(f"{maintenant:%Y-%m-%d %H:%M} aucune donnée intraday")
        return

    jour = derniere_bougie.date()
    relais = SUR_GITHUB and not bilan and not mac_actif(maintenant)
    if bilan:
        alertes = [bilan_cloture(variations, var_indice, fonda, resultats, jour)]
    elif SUR_GITHUB and not relais:
        alertes = [a for a in alertes if a[1] == "resultats"]  # le Mac s'occupe du reste
    elif depot:
        alertes = [a for a in alertes if a[1] != "resultats"]  # GitHub envoie le calendrier
    fichier_etat = DONNEES / f"etat-{jour}.json"
    etat = json.loads(fichier_etat.read_text()) if fichier_etat.exists() else {}
    nouvelles = filtrer_nouvelles(alertes, etat, maintenant, config["delai_rappel_15min_minutes"])
    if not nouvelles:
        return

    titre = f"🔔 Bilan de clôture du {jour:%d/%m}" if bilan else "📊 Nasdaq 100"
    entete = f"{titre} — {maintenant:%H:%M} New York / {maintenant.astimezone(PARIS):%H:%M} Paris"
    if var_indice is not None and not bilan:
        entete += f" | indice (QQQ) {var_indice:+.1f} %"
    if relais:
        entete += "\n(Mac injoignable : relais GitHub, retard possible de 10 à 30 min)"
    texte = "\n".join([entete, ""] + [a[2] for a in sorted(nouvelles)])
    if args.dry_run:
        print(texte)
        return

    try:
        envoyer_telegram(texte)
    except Exception as erreur:
        print(f"{maintenant:%Y-%m-%d %H:%M} échec de l'envoi Telegram : {erreur}", file=sys.stderr)
        sys.exit(1)

    DONNEES.mkdir(exist_ok=True)
    ALERTES.mkdir(exist_ok=True)
    for ancien in DONNEES.glob("etat-*.json"):
        if ancien != fichier_etat:
            ancien.unlink()
    with open(ALERTES / f"{jour}.jsonl", "a") as journal:
        for ticker, type_, message, prix in nouvelles:
            etat[f"{ticker}|{type_}"] = maintenant.isoformat()
            journal.write(json.dumps({"heure_ny": f"{maintenant:%H:%M}", "ticker": ticker, "type": type_,
                                      "prix": prix, "message": message}, ensure_ascii=False) + "\n")
    fichier_etat.write_text(json.dumps(etat, indent=1))
    print(f"{maintenant:%Y-%m-%d %H:%M} {len(nouvelles)} alerte(s) envoyée(s)")


if __name__ == "__main__":
    main()
