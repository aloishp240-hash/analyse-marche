import json
from datetime import date, timedelta

import pytest

from court_terme.alpaca import ErreurAlpaca
from inities import copie_alpaca
from inities.copie_alpaca import charger_etat, copier, entrees, rapprocher, sorties
from inities.modele import Operation

P = {"capital_initial": 3000, "part_par_achat_pct": 10, "part_max_une_action_pct": 15, "positions_max": 10,
     "stop_pct": 15, "duree_max_mois": 6, "dirigeant_montant_min": 100000, "groupe_dirigeants_jours": 30}
AUJOURD_HUI = date(2026, 10, 21)
ELU = "politicien:chambre:Dupont|Jean|CA-01"


class FauxClient:
    """Mémorise les ordres et les positions comme le ferait le compte paper ; aucun accès réseau."""

    def __init__(self, equity=3000, refuse=None):
        self.equity = equity
        self.refuse = refuse  # message d'ErreurAlpaca levé par passer_oto
        self.ordres_par_id, self.positions_alpaca, self.appels = {}, [], []

    def _noter(self, *appel):
        self.appels.append(appel)

    def appels_de(self, nom):
        return [a for a in self.appels if a[0] == nom]

    def compte(self):
        return {"equity": str(self.equity), "cash": str(self.equity)}

    def positions(self):
        return list(self.positions_alpaca)

    def ordre(self, identifiant):
        self._noter("ordre", identifiant)
        return self.ordres_par_id[identifiant]

    def passer_oto(self, ticker, quantite, stop, id_client, validite="gtc"):
        self._noter("passer_oto", ticker, quantite, stop, id_client, validite)
        if self.refuse:
            raise ErreurAlpaca(self.refuse)
        n = len(self.ordres_par_id) + 1
        ordre = {"id": f"ordre-{n}", "client_order_id": id_client, "symbol": ticker, "status": "accepted",
                 "filled_avg_price": None, "legs": [{"id": f"stop-{n}", "type": "stop", "status": "held",
                                                     "stop_price": f"{stop:.2f}"}]}
        self.ordres_par_id[ordre["id"]] = ordre
        return ordre

    def executer(self, identifiant, prix):
        """L'achat est exécuté à `prix` : le stop lié devient actif et la position apparaît."""
        ordre = self.ordres_par_id[identifiant]
        ordre.update(status="filled", filled_avg_price=str(prix))
        ordre["legs"][0]["status"] = "new"
        self.positions_alpaca.append({"symbol": ordre["symbol"], "qty": "3"})

    def stop_declenche(self, identifiant):
        ordre = self.ordres_par_id[identifiant]
        ordre["legs"][0]["status"] = "filled"
        self.positions_alpaca = [pos for pos in self.positions_alpaca if pos["symbol"] != ordre["symbol"]]

    def modifier_stop(self, identifiant, stop):
        self._noter("modifier_stop", identifiant, stop)
        return {"id": identifiant}

    def annuler(self, identifiant):
        self._noter("annuler", identifiant)

    def fermer_position(self, ticker):
        self._noter("fermer_position", ticker)
        self.positions_alpaca = [pos for pos in self.positions_alpaca if pos["symbol"] != ticker]
        return {"id": f"vente-{ticker}", "side": "sell", "status": "accepted"}


@pytest.fixture(autouse=True)
def chemins(tmp_path, monkeypatch):
    monkeypatch.setattr(copie_alpaca, "SUSPENSION", tmp_path / "suspension.json")
    monkeypatch.setattr(copie_alpaca, "POSITIONS", tmp_path / "positions.json")


def achat(ticker="AAA", source="chambre:20035551:1", pub=AUJOURD_HUI - timedelta(days=1), montant=50000.0,
          personne=ELU, famille="politicien", details=None):
    return Operation(famille=famille, personne_id=personne, personne="Jean Dupont", ticker=ticker, sens="achat",
                     date_operation=pub - timedelta(days=5), date_publication=pub, montant_min=montant,
                     montant_max=montant, source_id=source, details=details or {})


def vente(ticker="AAA", pub=AUJOURD_HUI, personne=ELU):
    return Operation(famille="politicien", personne_id=personne, personne="Jean Dupont", ticker=ticker,
                     sens="vente", date_operation=pub - timedelta(days=3), date_publication=pub, montant_min=1000.0,
                     montant_max=15000.0, source_id=f"chambre:99:{ticker}", details={})


def etat_vide():
    return {"positions": [], "historique": []}


def position(ticker="AAA", n=1, **champs):
    base = {"ticker": ticker, "personne_id": ELU, "source_id": f"chambre:{n}:1", "id_ordre": f"ordre-{n}",
            "id_stop": f"stop-{n}", "date_entree": "2026-10-01", "sortie_prevue": "2027-04-01", "stop_recale": False}
    return {**base, **champs}


def client_avec_position(ticker="AAA", prix=100, **champs):
    """Client dont l'ordre 1 est exécuté, et l'état qui le suit."""
    client = FauxClient()
    client.passer_oto(ticker, 3, prix * 0.85, "copie-x")
    client.executer("ordre-1", prix)
    client.appels.clear()
    return client, {"positions": [position(ticker, **champs)], "historique": []}


def suspension():
    return json.loads(copie_alpaca.SUSPENSION.read_text())


def test_entree_passe_un_oto_avec_stop_85():
    client = FauxClient()
    etat, messages, signales = entrees(etat_vide(), [achat()], {"AAA": 100.0}, client, P, AUJOURD_HUI)
    assert client.appels_de("passer_oto") == [("passer_oto", "AAA", 3, 85.0, "copie-chambre-20035551-1", "gtc")]
    assert etat["positions"] == [{
        "ticker": "AAA", "personne_id": ELU, "source_id": "chambre:20035551:1", "id_ordre": "ordre-1",
        "id_stop": "stop-1", "date_entree": "2026-10-21", "sortie_prevue": "2027-04-21", "stop_recale": False}]
    assert signales == [] and len(messages) == 1 and "AAA" in messages[0]


def test_id_client_coupe_a_48_caracteres():
    client = FauxClient()
    entrees(etat_vide(), [achat(source="13f:0001234567-26-000123:" + "9" * 40)], {"AAA": 100.0}, client, P,
            AUJOURD_HUI)
    id_client = client.appels_de("passer_oto")[0][4]
    assert len(id_client) == 48 and id_client.startswith("copie-13f-0001234567-26-000123-99")


def test_relance_ne_double_pas():
    client = FauxClient()
    etat, _, _ = entrees(etat_vide(), [achat()], {"AAA": 100.0}, client, P, AUJOURD_HUI)
    etat, messages, signales = entrees(etat, [achat()], {"AAA": 100.0}, client, P, AUJOURD_HUI)
    assert len(client.appels_de("passer_oto")) == 1 and len(etat["positions"]) == 1
    assert messages == []


def test_meme_operation_deja_sortie_n_est_pas_rachetee():
    client = FauxClient()
    etat = {"positions": [], "historique": [{"ticker": "AAA", "source_id": "chambre:20035551:1", "raison": "stop"}]}
    etat, _, signales = entrees(etat, [achat()], {"AAA": 100.0}, client, P, AUJOURD_HUI)
    assert client.appels == [] and etat["positions"] == []


def test_titre_deja_detenu_seulement_signale():
    client = FauxClient()
    etat = {"positions": [position("AAA")], "historique": []}
    etat, messages, signales = entrees(etat, [achat(source="chambre:2:1")], {"AAA": 100.0}, client, P, AUJOURD_HUI)
    assert client.appels_de("passer_oto") == []
    assert [(op.source_id, motif) for op, motif in signales] == [("chambre:2:1", "titre déjà détenu")]


def test_onzieme_position_seulement_signalee():
    client = FauxClient()
    etat = {"positions": [position(f"T{i}", n=i) for i in range(10)], "historique": []}
    etat, _, signales = entrees(etat, [achat(ticker="NEW")], {"NEW": 100.0}, client, P, AUJOURD_HUI)
    assert client.appels == [] and len(etat["positions"]) == 10
    assert [(op.ticker, motif) for op, motif in signales] == [("NEW", "plafond de 10 positions atteint")]


def test_quantite_zero_seulement_signalee():
    client = FauxClient()  # 10 % de 3000 = 300 ; une action à 600 dépasse aussi 15 % (450)
    etat, _, signales = entrees(etat_vide(), [achat()], {"AAA": 600.0}, client, P, AUJOURD_HUI)
    assert client.appels_de("passer_oto") == [] and etat["positions"] == []
    assert signales[0][1] == "une action coûte plus de 15 % du compte"


def test_une_action_si_elle_coute_moins_de_15_pour_cent():
    client = FauxClient()
    entrees(etat_vide(), [achat()], {"AAA": 400.0}, client, P, AUJOURD_HUI)
    assert client.appels_de("passer_oto")[0][2] == 1


def test_cours_manquant_seulement_signale():
    client = FauxClient()
    _, _, signales = entrees(etat_vide(), [achat()], {}, client, P, AUJOURD_HUI)
    assert client.appels_de("passer_oto") == [] and "cours" in signales[0][1]


def test_priorite_quand_une_seule_place_reste():
    client = FauxClient()
    etat = {"positions": [position(f"T{i}", n=i) for i in range(9)], "historique": []}
    vieux = achat(ticker="OLD", source="chambre:91:1", pub=AUJOURD_HUI - timedelta(days=3), montant=1000.0)
    gros = achat(ticker="GROS", source="chambre:92:1", montant=250000.0)
    petit = achat(ticker="PETIT", source="chambre:93:1", montant=20000.0)
    cours = {"OLD": 100.0, "GROS": 100.0, "PETIT": 100.0}
    etat, _, signales = entrees(etat, [petit, gros, vieux], cours, client, P, AUJOURD_HUI)
    assert [a[1] for a in client.appels_de("passer_oto")] == ["OLD"]
    assert {op.ticker for op, _ in signales} == {"GROS", "PETIT"}


def test_priorite_achats_groupes_puis_montant():
    client = FauxClient()
    etat = {"positions": [position(f"T{i}", n=i) for i in range(9)], "historique": []}
    detail = {"code": "P", "dirigeant": True}

    def dirigeant(ticker, source, cik, montant):
        return achat(ticker=ticker, source=source, montant=montant, famille="dirigeant",
                     personne=f"dirigeant:{cik}", details=detail)
    seul = dirigeant("SEUL", "form4:a:0", 1, 900000.0)  # plus gros, mais pas en groupe
    g1, g2 = dirigeant("GRP", "form4:b:0", 2, 150000.0), dirigeant("GRP", "form4:c:0", 3, 140000.0)
    cours = {"SEUL": 100.0, "GRP": 100.0}
    etat, _, signales = entrees(etat, [seul, g1, g2], cours, client, P, AUJOURD_HUI)
    assert [a[1] for a in client.appels_de("passer_oto")] == ["GRP"]
    assert {op.source_id for op, _ in signales} == {"form4:a:0", "form4:c:0"}


def test_recalage_unique_du_stop():
    client, etat = client_avec_position(prix=100)
    client.ordres_par_id["ordre-1"]["filled_avg_price"] = "104.5"
    etat, messages = rapprocher(etat, client, P)
    assert client.appels_de("modifier_stop") == [("modifier_stop", "stop-1", 104.5 * 0.85)]
    assert etat["positions"][0]["stop_recale"] is True and len(messages) == 1
    etat, messages = rapprocher(etat, client, P)
    assert len(client.appels_de("modifier_stop")) == 1 and messages == []


def test_ordre_pas_encore_execute_attend():
    client = FauxClient()
    client.passer_oto("AAA", 3, 85.0, "copie-x")
    etat = {"positions": [position()], "historique": []}
    nouvel, messages = rapprocher(etat, client, P)
    assert nouvel == etat and messages == [] and not copie_alpaca.SUSPENSION.exists()
    assert client.appels_de("modifier_stop") == []


def test_stop_execute_donne_une_sortie():
    client, etat = client_avec_position()
    client.stop_declenche("ordre-1")
    etat, messages = rapprocher(etat, client, P)
    assert etat["positions"] == []
    assert etat["historique"][0]["ticker"] == "AAA" and etat["historique"][0]["raison"] == "stop"
    assert len(messages) == 1 and "stop" in messages[0].lower() and not copie_alpaca.SUSPENSION.exists()


def test_vente_de_la_personne_annule_le_stop_puis_ferme():
    client, etat = client_avec_position()
    etat, messages = sorties(etat, [vente()], AUJOURD_HUI, client)
    assert [a[0] for a in client.appels if a[0] in ("annuler", "fermer_position")] == ["annuler", "fermer_position"]
    assert client.appels_de("annuler") == [("annuler", "stop-1")]
    assert client.appels_de("fermer_position") == [("fermer_position", "AAA")]
    assert etat["positions"] == [] and etat["historique"][0]["raison"] == "vente"
    assert etat["historique"][0]["id_vente"] == "vente-AAA" and len(messages) == 1


def test_vente_d_une_autre_personne_ou_anterieure_a_l_entree_ignoree():
    client, etat = client_avec_position()
    ventes = [vente(personne="politicien:chambre:Autre|Paul|TX-02"), vente(ticker="BBB"),
              vente(pub=date(2026, 9, 30))]
    nouvel, messages = sorties(etat, ventes, AUJOURD_HUI, client)
    assert nouvel == etat and messages == [] and client.appels == []


def test_six_mois_ferme():
    client, etat = client_avec_position(sortie_prevue="2026-10-21")
    etat, messages = sorties(etat, [], AUJOURD_HUI, client)
    assert client.appels_de("fermer_position") == [("fermer_position", "AAA")]
    assert etat["historique"][0]["raison"] == "6 mois"


def test_six_mois_pas_encore_atteints():
    client, etat = client_avec_position(sortie_prevue="2026-10-22")
    etat, _ = sorties(etat, [], AUJOURD_HUI, client)
    assert client.appels == [] and len(etat["positions"]) == 1


def test_sortie_impossible_suspend_et_garde_la_position():
    client, etat = client_avec_position(sortie_prevue="2026-10-21")
    client.fermer_position = lambda ticker: (_ for _ in ()).throw(ErreurAlpaca("403 qty held"))
    etat, messages = sorties(etat, [], AUJOURD_HUI, client)
    assert len(etat["positions"]) == 1 and copie_alpaca.SUSPENSION.exists()
    assert "AAA" in messages[0] and "stop" in messages[0]


def test_ordre_rejete_suspend():
    client = FauxClient(refuse="403 insufficient buying power")
    achats = [achat(), achat(ticker="BBB", source="chambre:20035552:1")]
    messages, signales = copier(achats, [], {"AAA": 100.0, "BBB": 100.0}, client, P, AUJOURD_HUI)
    assert copie_alpaca.SUSPENSION.exists() and "insufficient buying power" in suspension()["motif"]
    assert len(client.appels_de("passer_oto")) == 1
    assert any("suspendue" in m for m in messages) and any("insufficient buying power" in m for m in messages)
    assert [op.ticker for op, _ in signales] == ["AAA", "BBB"]
    assert charger_etat()["positions"] == []


def test_position_fermee_a_la_main_suspend():
    client, etat = client_avec_position()
    client.positions_alpaca = []
    etat, messages = rapprocher(etat, client, P)
    assert copie_alpaca.SUSPENSION.exists() and "AAA" in suspension()["motif"]
    assert len(messages) == 1 and "suspendue" in messages[0]
    assert len(etat["positions"]) == 1


def test_position_alpaca_inconnue_suspend():
    client = FauxClient()
    client.positions_alpaca = [{"symbol": "ZZZ", "qty": "5"}]
    _, messages = rapprocher(etat_vide(), client, P)
    assert copie_alpaca.SUSPENSION.exists() and "ZZZ" in messages[0]


def test_jambe_stop_manquante_suspend():
    client, etat = client_avec_position()
    client.ordres_par_id["ordre-1"]["legs"] = []
    _, messages = rapprocher(etat, client, P)
    assert copie_alpaca.SUSPENSION.exists() and "stop" in messages[0]
    assert client.appels_de("modifier_stop") == []


def test_ordre_d_achat_annule_suspend():
    client, etat = client_avec_position()
    client.ordres_par_id["ordre-1"]["status"] = "canceled"
    client.positions_alpaca = []
    _, messages = rapprocher(etat, client, P)
    assert copie_alpaca.SUSPENSION.exists()


def test_vente_en_cours_n_est_pas_un_ecart_puis_se_confirme():
    client, etat = client_avec_position(sortie_prevue="2026-10-21")
    etat, _ = sorties(etat, [], AUJOURD_HUI, client)
    client.positions_alpaca = [{"symbol": "AAA", "qty": "3"}]  # la vente n'est pas encore exécutée
    client.ordres_par_id["vente-AAA"] = {"id": "vente-AAA", "status": "accepted"}
    etat, messages = rapprocher(etat, client, P)
    assert not copie_alpaca.SUSPENSION.exists() and messages == []
    client.positions_alpaca = []
    client.ordres_par_id["vente-AAA"]["status"] = "filled"
    etat, _ = rapprocher(etat, client, P)
    assert etat["historique"][0]["vente_confirmee"] is True and not copie_alpaca.SUSPENSION.exists()


def test_suspendu_ne_passe_rien():
    client, etat = client_avec_position(sortie_prevue="2026-10-21")
    copie_alpaca.enregistrer_etat(etat)
    copie_alpaca.SUSPENSION.write_text(json.dumps({"depuis": "2026-10-20", "motif": "test"}))
    messages, signales = copier([achat(ticker="BBB", source="chambre:5:1")], [vente()], {"BBB": 100.0}, client, P,
                                AUJOURD_HUI)
    assert client.appels_de("passer_oto") == [] and client.appels_de("fermer_position") == []
    assert client.appels_de("annuler") == []
    assert client.appels_de("modifier_stop")  # le rapprochement, lui, continue
    assert signales == [] and len(charger_etat()["positions"]) == 1


def test_copier_enchaine_et_enregistre_l_etat():
    client, etat = client_avec_position("AAA", sortie_prevue="2026-10-21")
    copie_alpaca.enregistrer_etat(etat)
    messages, signales = copier([achat(ticker="BBB", source="chambre:7:1")], [], {"BBB": 50.0}, client, P,
                                AUJOURD_HUI)
    enregistre = charger_etat()
    assert [pos["ticker"] for pos in enregistre["positions"]] == ["BBB"]
    assert enregistre["historique"][0]["ticker"] == "AAA" and len(messages) == 3  # recalage, sortie, entrée
    assert client.appels_de("modifier_stop") and client.appels_de("fermer_position")


def test_ecart_detecte_empeche_sorties_et_entrees():
    client, etat = client_avec_position(sortie_prevue="2026-10-21")
    client.positions_alpaca = []
    copie_alpaca.enregistrer_etat(etat)
    copier([achat(ticker="BBB", source="chambre:7:1")], [], {"BBB": 50.0}, client, P, AUJOURD_HUI)
    assert client.appels_de("passer_oto") == [] and client.appels_de("fermer_position") == []


def test_etat_absent_est_vide():
    assert charger_etat() == etat_vide()
