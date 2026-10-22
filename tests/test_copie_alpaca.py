import hashlib
import json
import os
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
        self.plante_au_appel = None  # numéro de l'appel de passer_oto qui lève un KeyError
        self.refuse_modification = self.refuse_fermeture = self.refuse_restop = None
        self.remplacements = 0
        self.ordres_par_id, self.positions_alpaca, self.appels = {}, [], []
        self.ids_clients = set()
        self.annulation = None  # statuts successifs du stop après son annulation (sinon « canceled » aussitôt)
        self.reste_a_lire = {}

    def _noter(self, *appel):
        self.appels.append(appel)

    def appels_de(self, nom):
        return [a for a in self.appels if a[0] == nom]

    def compte(self):
        return {"equity": str(self.equity), "cash": str(self.equity)}

    def positions(self):
        return list(self.positions_alpaca)

    def _trouver(self, identifiant):
        if identifiant in self.ordres_par_id:
            return self.ordres_par_id[identifiant]
        for ordre in self.ordres_par_id.values():
            for jambe in ordre.get("legs", []):
                if jambe["id"] == identifiant:
                    return jambe
        raise ErreurAlpaca("Alpaca a répondu 404 : order not found")

    def ordre(self, identifiant):
        self._noter("ordre", identifiant)
        ordre = self._trouver(identifiant)
        file = self.reste_a_lire.get(identifiant)
        if file:
            ordre["status"] = file.pop(0) if len(file) > 1 else file[0]
        return ordre

    def ordre_par_id_client(self, id_client):
        self._noter("ordre_par_id_client", id_client)
        for ordre in self.ordres_par_id.values():
            if ordre.get("client_order_id") == id_client:
                return ordre
        raise ErreurAlpaca("Alpaca a répondu 404 : order not found")

    def passer_oto(self, ticker, quantite, stop, id_client, validite="gtc"):
        self._noter("passer_oto", ticker, quantite, stop, id_client, validite)
        if self.plante_au_appel == len(self.appels_de("passer_oto")):
            raise KeyError("id")
        if self.refuse:
            raise ErreurAlpaca(self.refuse)
        if id_client in self.ids_clients:
            raise ErreurAlpaca("Alpaca a répondu 422 : client_order_id must be unique")
        self.ids_clients.add(id_client)
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

    def stop_declenche(self, identifiant, prix=None):
        ordre = self.ordres_par_id[identifiant]
        ordre["legs"][0]["status"] = "filled"
        ordre["legs"][0]["filled_avg_price"] = str(prix if prix is not None else ordre["legs"][0]["stop_price"])
        self.positions_alpaca = [pos for pos in self.positions_alpaca if pos["symbol"] != ordre["symbol"]]

    def modifier_stop(self, identifiant, stop):
        """Comme Alpaca, remplacer un ordre en crée un nouveau : l'ancien devient « replaced »."""
        self._noter("modifier_stop", identifiant, stop)
        if self.refuse_modification:
            raise ErreurAlpaca(self.refuse_modification)
        self.remplacements += 1
        nouvel = f"{identifiant}-r{self.remplacements}"
        for ordre in self.ordres_par_id.values():
            for jambe in ordre.get("legs", []):
                if jambe["id"] == identifiant:
                    jambe["status"] = "replaced"
                    ordre["legs"].append({"id": nouvel, "type": "stop", "status": "new", "stop_price": f"{stop:.2f}"})
                    return {"id": nouvel}
        return {"id": nouvel}

    def passer_stop(self, ticker, quantite, stop, id_client, validite="gtc"):
        self._noter("passer_stop", ticker, quantite, stop, id_client, validite)
        if self.refuse_restop:
            raise ErreurAlpaca(self.refuse_restop)
        self.ordres_par_id["restop-1"] = {"id": "restop-1", "client_order_id": id_client, "symbol": ticker,
                                          "type": "stop", "status": "new", "stop_price": f"{stop:.2f}"}
        return {"id": "restop-1"}

    def annuler(self, identifiant):
        self._noter("annuler", identifiant)
        if self.annulation:
            self.reste_a_lire[identifiant] = list(self.annulation)
        else:
            self._trouver(identifiant)["status"] = "canceled"

    def fermer_position(self, ticker):
        self._noter("fermer_position", ticker)
        if self.refuse_fermeture:
            raise ErreurAlpaca(self.refuse_fermeture)
        self.positions_alpaca = [pos for pos in self.positions_alpaca if pos["symbol"] != ticker]
        return {"id": f"vente-{ticker}", "side": "sell", "status": "accepted", "filled_avg_price": None}


@pytest.fixture(autouse=True)
def chemins(tmp_path, monkeypatch):
    monkeypatch.setattr(copie_alpaca, "SUSPENSION", tmp_path / "suspension.json")
    monkeypatch.setattr(copie_alpaca, "POSITIONS", tmp_path / "positions.json")


def achat(ticker="AAA", source="chambre:20035551:1", pub=AUJOURD_HUI - timedelta(days=1), montant=50000.0,
          personne=ELU, famille="politicien", details=None):
    return Operation(famille=famille, personne_id=personne, personne="Jean Dupont", ticker=ticker, sens="achat",
                     date_operation=pub - timedelta(days=5), date_publication=pub, montant_min=montant,
                     montant_max=montant, source_id=source, details=details or {})


def vente(ticker="AAA", pub=AUJOURD_HUI, personne=ELU, operation=None):
    return Operation(famille="politicien", personne_id=personne, personne="Jean Dupont", ticker=ticker,
                     sens="vente", date_operation=operation or pub - timedelta(days=3), date_publication=pub,
                     montant_min=1000.0,
                     montant_max=15000.0, source_id=f"chambre:99:{ticker}", details={})


def etat_vide():
    return {"positions": [], "historique": []}


def position(ticker="AAA", n=1, **champs):
    base = {"ticker": ticker, "personne_id": ELU, "source_id": f"chambre:{n}:1", "id_ordre": f"ordre-{n}",
            "id_stop": f"stop-{n}", "date_entree": "2026-10-01", "sortie_prevue": "2027-04-01", "stop_recale": False,
            "stop": 85.0, "quantite": 3}
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
        "id_stop": "stop-1", "date_entree": "2026-10-21", "sortie_prevue": "2027-04-21", "stop_recale": False,
        "stop": 85.0, "quantite": 3, "date_operation": "2026-10-15"}]
    assert signales == [] and len(messages) == 1 and "AAA" in messages[0]


def test_id_client_distincts_pour_deux_rangs_du_senat():
    uuid = "0123456789abcdef0123456789abcdef0123"[:36].ljust(36, "0")
    client = FauxClient()
    achats = [achat(ticker="AAA", source=f"senat:{uuid}:1"), achat(ticker="BBB", source=f"senat:{uuid}:2")]
    entrees(etat_vide(), achats, {"AAA": 100.0, "BBB": 100.0}, client, P, AUJOURD_HUI)
    ids = [a[4] for a in client.appels_de("passer_oto")]
    assert len(set(ids)) == 2 and ids[0] == f"copie-senat-{uuid}-1"


def test_id_client_long_est_condense_et_stable():
    source = "13f:" + "9" * 196
    assert len(source) == 200
    id_client = copie_alpaca._id_client(source)
    assert len(id_client) <= 128 and id_client == copie_alpaca._id_client(source)
    assert id_client == "copie-" + hashlib.sha256(source.encode()).hexdigest()[:40]
    assert copie_alpaca._id_client(source + "x") != id_client


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
    client.refuse_fermeture = "403 qty held"
    client.refuse_restop = "422 refusé"
    etat, messages = sorties(etat, [], AUJOURD_HUI, client)
    assert len(etat["positions"]) == 1 and copie_alpaca.SUSPENSION.exists()
    assert "AAA" in messages[0] and "non reposé" in messages[0] and "qty held" in messages[0]


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
    client, etat = client_avec_position(id_stop=None)
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


def test_exception_inattendue_garde_l_ordre_passe_et_suspend():
    client = FauxClient()
    client.plante_au_appel = 2
    achats = [achat(), achat(ticker="BBB", source="chambre:20035552:1")]
    with pytest.raises(KeyError):
        copier(achats, [], {"AAA": 100.0, "BBB": 100.0}, client, P, AUJOURD_HUI)
    assert [pos["ticker"] for pos in charger_etat()["positions"]] == ["AAA"]
    assert copie_alpaca.SUSPENSION.exists() and "KeyError" in suspension()["motif"]


def test_erreur_alpaca_hors_ordre_ne_suspend_pas_mais_garde_l_etat():
    client = FauxClient()
    client.compte = lambda: (_ for _ in ()).throw(ErreurAlpaca("Alpaca injoignable"))
    with pytest.raises(ErreurAlpaca):
        copier([achat()], [], {"AAA": 100.0}, client, P, AUJOURD_HUI)
    assert not copie_alpaca.SUSPENSION.exists()


def test_recalage_en_echec_suspend_sans_planter():
    client, etat = client_avec_position()
    client.refuse_modification = "422 stop invalide"
    etat, messages = rapprocher(etat, client, P)
    assert etat["positions"][0]["stop_recale"] is False and etat["positions"][0]["stop"] == 85.0
    assert copie_alpaca.SUSPENSION.exists() and "stop invalide" in suspension()["motif"]
    assert len(messages) == 1 and "AAA" in messages[0]


def test_recalage_puis_sortie_utilise_le_nouvel_id_de_stop():
    client, etat = client_avec_position(sortie_prevue="2026-10-21")
    etat, _ = rapprocher(etat, client, P)
    assert etat["positions"][0]["id_stop"] == "stop-1-r1" and etat["positions"][0]["stop"] == 85.0
    etat, _ = rapprocher(etat, client, P)  # la jambe remplacée ne ré-écrase pas l'identifiant
    assert etat["positions"][0]["id_stop"] == "stop-1-r1" and not copie_alpaca.SUSPENSION.exists()
    sorties(etat, [], AUJOURD_HUI, client)
    assert client.appels_de("annuler") == [("annuler", "stop-1-r1")]


def test_recalage_met_a_jour_le_prix_du_stop_de_l_etat():
    client, etat = client_avec_position()
    client.ordres_par_id["ordre-1"]["filled_avg_price"] = "104"
    etat, _ = rapprocher(etat, client, P)
    assert etat["positions"][0]["stop"] == pytest.approx(104 * 0.85)


def test_sortie_avant_execution_annule_l_ordre_parent():
    client = FauxClient()
    client.passer_oto("AAA", 3, 85.0, "copie-x")  # accepté, pas exécuté, pas de position
    client.appels.clear()
    etat = {"positions": [position()], "historique": []}
    etat, messages = sorties(etat, [vente()], AUJOURD_HUI, client)
    assert client.appels_de("annuler") == [("annuler", "ordre-1")]
    assert client.appels_de("fermer_position") == []
    assert etat["positions"] == []
    ligne = etat["historique"][0]
    assert ligne["raison"] == "vente" and ligne["id_vente"] is None and ligne["vente_confirmee"] is True
    assert len(messages) == 1


def test_sortie_avant_execution_complete_ferme_la_part_deja_achetee():
    client = FauxClient()
    client.passer_oto("AAA", 3, 85.0, "copie-x")
    client.ordres_par_id["ordre-1"]["status"] = "partially_filled"
    client.positions_alpaca = [{"symbol": "AAA", "qty": "1"}]
    client.appels.clear()
    etat = {"positions": [position()], "historique": []}
    etat, _ = sorties(etat, [vente()], AUJOURD_HUI, client)
    assert client.appels_de("annuler") == [("annuler", "ordre-1")]
    assert client.appels_de("fermer_position") == [("fermer_position", "AAA")]
    assert etat["historique"][0]["id_vente"] == "vente-AAA" and etat["historique"][0]["vente_confirmee"] is False


def test_fermeture_refusee_repose_le_stop_une_fois():
    client, etat = client_avec_position(sortie_prevue="2026-10-21", stop=88.5)
    client.refuse_fermeture = "403 qty held"
    etat, messages = sorties(etat, [], AUJOURD_HUI, client)
    assert client.appels_de("passer_stop") == [
        ("passer_stop", "AAA", 3, 88.5, "copie-restop-20261021-copie-chambre-1-1", "gtc")]
    assert len(client.appels_de("fermer_position")) == 1
    assert etat["positions"][0]["id_stop"] == "restop-1"
    assert copie_alpaca.SUSPENSION.exists() and "reposé" in messages[0] and "non reposé" not in messages[0]


def test_fermeture_refusee_et_restop_refuse():
    client, etat = client_avec_position(sortie_prevue="2026-10-21")
    client.refuse_fermeture, client.refuse_restop = "403 qty held", "422 refusé"
    etat, messages = sorties(etat, [], AUJOURD_HUI, client)
    assert len(client.appels_de("passer_stop")) == 1
    assert etat["positions"][0]["id_stop"] == "stop-1" and "non reposé" in messages[0]
    assert copie_alpaca.SUSPENSION.exists()


def test_fermeture_refusee_sans_stop_annule_ne_repose_rien():
    client, etat = client_avec_position(sortie_prevue="2026-10-21", id_stop=None)
    client.refuse_fermeture = "403 qty held"
    sorties(etat, [], AUJOURD_HUI, client)
    assert client.appels_de("passer_stop") == [] and client.appels_de("annuler") == []


def test_pas_de_rachat_le_jour_d_une_sortie():
    client, etat = client_avec_position(sortie_prevue="2026-10-21")
    copie_alpaca.enregistrer_etat(etat)
    messages, signales = copier([achat(ticker="AAA", source="chambre:8:1")], [], {"AAA": 100.0}, client, P,
                                AUJOURD_HUI)
    assert client.appels_de("fermer_position") and client.appels_de("passer_oto") == []
    assert [(op.source_id, motif) for op, motif in signales] == [("chambre:8:1", "titre sorti aujourd'hui")]
    assert charger_etat()["positions"] == []


def test_jambe_annulee_n_ecrase_pas_le_stop_repose():
    client, etat = client_avec_position(id_stop="restop-1", stop_recale=True)
    client.ordres_par_id["ordre-1"]["legs"][0]["status"] = "canceled"
    client.passer_stop("AAA", 3, 85.0, "copie-restop-x")
    etat, _ = rapprocher(etat, client, P)
    assert etat["positions"][0]["id_stop"] == "restop-1" and not copie_alpaca.SUSPENSION.exists()


# --- lecture directe du stop (I4) ---

def test_le_stop_est_lu_directement_et_non_dans_les_jambes_de_l_achat():
    client, etat = client_avec_position()
    client.ordres_par_id["ordre-1"]["legs"] = []  # l'ordre d'achat ne détaille plus ses jambes
    client.ordres_par_id["stop-1"] = {"id": "stop-1", "type": "stop", "status": "new", "stop_price": "85.00"}
    etat, messages = rapprocher(etat, client, P)
    assert ("ordre", "stop-1") in client.appels and not copie_alpaca.SUSPENSION.exists()
    assert client.appels_de("modifier_stop") == [("modifier_stop", "stop-1", 100 * 0.85)]


def test_stop_execute_lu_sur_son_propre_ordre():
    client, etat = client_avec_position()
    client.ordres_par_id["stop-1"] = {"id": "stop-1", "type": "stop", "status": "filled", "stop_price": "85.00",
                                      "filled_avg_price": "84.9"}
    client.positions_alpaca = []
    etat, messages = rapprocher(etat, client, P)
    assert etat["positions"] == [] and etat["historique"][0]["raison"] == "stop"
    assert "84.90" in messages[0] and not copie_alpaca.SUSPENSION.exists()


def test_stop_remplace_ailleurs_suspend():
    client, etat = client_avec_position()
    client.ordres_par_id["ordre-1"]["legs"][0]["status"] = "replaced"
    _, messages = rapprocher(etat, client, P)
    assert copie_alpaca.SUSPENSION.exists() and "replaced" in suspension()["motif"]


def test_sans_id_de_stop_on_le_prend_dans_les_jambes_une_fois():
    client, etat = client_avec_position(id_stop=None)
    etat, _ = rapprocher(etat, client, P)
    assert etat["positions"][0]["id_stop"] == "stop-1-r1"  # lu sur la jambe, puis recalé
    assert not copie_alpaca.SUSPENSION.exists()


def test_stop_repose_est_lu_ensuite_par_son_identifiant():
    client, etat = client_avec_position(sortie_prevue="2026-10-21")
    client.refuse_fermeture = "403 qty held"
    etat, _ = sorties(etat, [], AUJOURD_HUI, client)
    assert etat["positions"][0]["id_stop"] == "restop-1"
    client.appels.clear()
    rapprocher(etat, client, P)
    assert ("ordre", "restop-1") in client.appels


# --- annulation du stop avant la vente (I5) ---

def test_vente_attend_que_le_stop_soit_annule():
    client, etat = client_avec_position()
    client.annulation = ["pending_cancel", "pending_cancel", "canceled"]
    pauses = []
    etat, messages = sorties(etat, [vente()], AUJOURD_HUI, client, pause=pauses.append)
    lectures = [a for a in client.appels if a == ("ordre", "stop-1")]
    assert len(lectures) == 3 and pauses == [0.5, 0.5]
    noms = [a[0] for a in client.appels]
    assert noms.index("annuler") < noms.index("fermer_position")
    assert etat["historique"][0]["id_vente"] == "vente-AAA" and not copie_alpaca.SUSPENSION.exists()


def test_stop_jamais_annule_pas_de_vente():
    client, etat = client_avec_position()
    client.annulation = ["pending_cancel"]
    pauses = []
    etat, messages = sorties(etat, [vente()], AUJOURD_HUI, client, pause=pauses.append)
    assert len([a for a in client.appels if a == ("ordre", "stop-1")]) == 10 and len(pauses) == 9
    assert client.appels_de("fermer_position") == [] and client.appels_de("passer_stop") == []
    assert len(etat["positions"]) == 1 and copie_alpaca.SUSPENSION.exists()
    assert "AAA" in messages[0] and "pending_cancel" in messages[0]


def test_stop_execute_pendant_l_attente_est_une_sortie_par_stop():
    client, etat = client_avec_position()
    client.annulation = ["pending_cancel", "filled"]
    client.ordres_par_id["ordre-1"]["legs"][0]["filled_avg_price"] = "85"
    etat, messages = sorties(etat, [vente()], AUJOURD_HUI, client, pause=lambda s: None)
    assert client.appels_de("fermer_position") == [] and etat["positions"] == []
    assert etat["historique"][0]["raison"] == "stop" and not copie_alpaca.SUSPENSION.exists()
    assert len(messages) == 1 and "stop" in messages[0].lower() and "-15.0 %" in messages[0]


# --- messages de sortie perdus (M1) ---

def test_erreur_alpaca_apres_une_sortie_garde_ses_messages():
    client, etat = client_avec_position("AAA")
    client.stop_declenche("ordre-1", 80)
    etat["positions"].append(position("BBB", n=9))  # son ordre n'existe pas chez Alpaca : 404
    copie_alpaca.enregistrer_etat(etat)
    with pytest.raises(ErreurAlpaca) as exc:
        copier([], [], {}, client, P, AUJOURD_HUI)
    assert len(exc.value.messages) == 1 and "AAA" in exc.value.messages[0] and "-20.0 %" in exc.value.messages[0]
    enregistre = charger_etat()
    assert [pos["ticker"] for pos in enregistre["positions"]] == ["BBB"]
    assert enregistre["historique"][0]["ticker"] == "AAA"


def test_exception_inattendue_garde_aussi_les_messages():
    client = FauxClient()
    client.plante_au_appel = 2
    achats = [achat(), achat(ticker="BBB", source="chambre:20035552:1")]
    with pytest.raises(KeyError) as exc:
        copier(achats, [], {"AAA": 100.0, "BBB": 100.0}, client, P, AUJOURD_HUI)
    assert len(exc.value.messages) == 1 and "AAA" in exc.value.messages[0]


# --- identifiant client déjà utilisé (M2) ---

def test_identifiant_client_deja_utilise_reprend_l_ordre_existant():
    client = FauxClient()
    client.passer_oto("AAA", 3, 85.0, "copie-chambre-20035551-1")  # passé, mais l'état n'a pas été enregistré
    client.appels.clear()
    etat, messages, signales = entrees(etat_vide(), [achat()], {"AAA": 100.0}, client, P, AUJOURD_HUI)
    assert client.appels_de("ordre_par_id_client") == [("ordre_par_id_client", "copie-chambre-20035551-1")]
    assert signales == [] and not copie_alpaca.SUSPENSION.exists()
    assert etat["positions"][0]["id_ordre"] == "ordre-1" and etat["positions"][0]["id_stop"] == "stop-1"
    assert len(client.ordres_par_id) == 1 and len(messages) == 1 and "AAA" in messages[0]


def test_identifiant_client_deja_utilise_mais_ordre_introuvable_suspend():
    client = FauxClient()
    client.ids_clients.add("copie-chambre-20035551-1")  # refusé, mais l'ordre n'est pas retrouvé
    etat, messages, signales = entrees(etat_vide(), [achat()], {"AAA": 100.0}, client, P, AUJOURD_HUI)
    assert etat["positions"] == [] and copie_alpaca.SUSPENSION.exists()
    assert [motif for _, motif in signales] == ["ordre refusé par Alpaca"]


def test_ordre_retrouve_d_un_autre_titre_est_refuse():
    client = FauxClient()
    client.passer_oto("ZZZ", 3, 85.0, "copie-chambre-20035551-1")
    etat, _, signales = entrees(etat_vide(), [achat()], {"AAA": 100.0}, client, P, AUJOURD_HUI)
    assert etat["positions"] == [] and copie_alpaca.SUSPENSION.exists()


def test_autre_refus_ne_cherche_pas_l_ordre():
    client = FauxClient(refuse="403 insufficient buying power")
    entrees(etat_vide(), [achat()], {"AAA": 100.0}, client, P, AUJOURD_HUI)
    assert client.appels_de("ordre_par_id_client") == []


# --- écritures atomiques (M4) ---

def test_etat_et_suspension_sont_ecrits_de_facon_atomique(monkeypatch, tmp_path):
    copie_alpaca.enregistrer_etat({"positions": [position()], "historique": []})
    copie_alpaca._suspendre("premier motif", AUJOURD_HUI)
    avant = (copie_alpaca.POSITIONS.read_text(), copie_alpaca.SUSPENSION.read_text())

    def plante(source, destination):
        raise OSError("disque plein")

    monkeypatch.setattr(copie_alpaca.os, "replace", plante)
    with pytest.raises(OSError):
        copie_alpaca.enregistrer_etat(etat_vide())
    copie_alpaca.SUSPENSION.unlink()
    with pytest.raises(OSError):
        copie_alpaca._suspendre("autre motif", AUJOURD_HUI)
    assert not copie_alpaca.SUSPENSION.exists()
    assert copie_alpaca.POSITIONS.read_text() == avant[0]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["positions.json"]  # aucun fichier temporaire


# --- messages de résultat (M6) ---

def test_message_d_entree_donne_prix_et_stop():
    client = FauxClient()
    _, messages, _ = entrees(etat_vide(), [achat()], {"AAA": 100.0}, client, P, AUJOURD_HUI)
    assert messages == ["📥 ACHAT COPIÉ — AAA\n3 actions à environ 100.00 $ (compte paper)\nStop posé à 85.00 $"]


def test_stop_execute_donne_le_resultat():
    client, etat = client_avec_position(prix=100)
    client.ordres_par_id["ordre-1"]["filled_avg_price"] = "100"
    client.stop_declenche("ordre-1", 84)
    etat, messages = rapprocher(etat, client, P)
    assert messages == ["📤 VENTE — AAA : stop exécuté\nEntrée 100.00 $, sortie 84.00 $ : -16.0 % (-48.00 $ sur 3 actions)"]


def test_vente_demandee_puis_resultat_a_l_execution():
    client, etat = client_avec_position(prix=100)
    client.ordres_par_id["ordre-1"]["filled_avg_price"] = "100"
    etat, messages = sorties(etat, [vente()], AUJOURD_HUI, client)
    assert len(messages) == 1 and "AAA" in messages[0] and "résultat" in messages[0]
    ligne = etat["historique"][0]
    assert ligne["prix_entree"] == 100.0 and ligne["quantite"] == 3 and ligne["vente_confirmee"] is False
    client.ordres_par_id["vente-AAA"] = {"id": "vente-AAA", "status": "filled", "filled_avg_price": "110"}
    etat, messages = rapprocher(etat, client, P)
    assert messages == ["📤 VENTE — AAA : vente publiée par la personne copiée\n"
                        "Entrée 100.00 $, sortie 110.00 $ : +10.0 % (+30.00 $ sur 3 actions)"]
    etat, messages = rapprocher(etat, client, P)
    assert messages == []  # annoncé une seule fois


def test_vente_avant_execution_de_l_achat_sans_resultat():
    client = FauxClient()
    client.passer_oto("AAA", 3, 85.0, "copie-x")
    etat = {"positions": [position()], "historique": []}
    etat, messages = sorties(etat, [vente()], AUJOURD_HUI, client)
    assert "résultat n/d" in messages[0] and "AAA" in messages[0]


# --- vente antérieure à l'achat, achat déjà revendu (P4) ---

def test_vente_anterieure_a_l_achat_copie_est_ignoree():
    client, etat = client_avec_position(date_operation="2026-10-10")
    ancienne = vente(pub=AUJOURD_HUI, operation=date(2026, 10, 5))  # publiée tard, mais faite avant l'achat
    etat, messages = sorties(etat, [ancienne], AUJOURD_HUI, client)
    assert len(etat["positions"]) == 1 and messages == [] and client.appels_de("annuler") == []


def test_vente_posterieure_a_l_achat_copie_sort_meme_publiee_avant_l_entree():
    client, etat = client_avec_position(date_operation="2026-10-10", date_entree="2026-10-20")
    vendue = vente(pub=date(2026, 10, 19), operation=date(2026, 10, 12))  # publiée avant notre entrée
    etat, messages = sorties(etat, [vendue], AUJOURD_HUI, client)
    assert etat["positions"] == [] and etat["historique"][0]["raison"] == "vente"


def test_position_sans_date_d_operation_garde_l_ancienne_regle():
    client, etat = client_avec_position()  # date_entree 2026-10-01, pas de date_operation
    avant = vente(pub=date(2026, 9, 30), operation=date(2026, 9, 28))
    etat, _ = sorties(etat, [avant], AUJOURD_HUI, client)
    assert len(etat["positions"]) == 1
    apres = vente(pub=date(2026, 10, 2), operation=date(2026, 9, 28))
    etat, _ = sorties(etat, [apres], AUJOURD_HUI, client)
    assert etat["positions"] == []


def test_vente_anterieure_reconnue():
    assert copie_alpaca.vente_anterieure(position(date_operation="2026-10-10"), vente(operation=date(2026, 10, 9)))
    assert not copie_alpaca.vente_anterieure(position(date_operation="2026-10-10"), vente(operation=date(2026, 10, 10)))
    assert not copie_alpaca.vente_anterieure(position(), vente(operation=date(2020, 1, 1)))  # date inconnue


def test_achat_deja_revendu_dans_le_meme_lot_est_signale():
    client = FauxClient()
    revente = vente(operation=date(2026, 10, 18))  # l'achat date du 15 octobre
    etat, messages, signales = entrees(etat_vide(), [achat()], {"AAA": 100.0}, client, P, AUJOURD_HUI,
                                       ventes=[revente])
    assert client.appels_de("passer_oto") == [] and etat["positions"] == []
    assert [(op.ticker, motif) for op, motif in signales] == [("AAA", "déjà revendu")]
    assert not copie_alpaca.SUSPENSION.exists()


def test_achat_marque_deja_revendu_par_la_collecte_est_signale():
    client = FauxClient()
    etat, _, signales = entrees(etat_vide(), [achat(details={"deja_revendu": True})], {"AAA": 100.0}, client, P,
                                AUJOURD_HUI)
    assert client.appels_de("passer_oto") == [] and etat["positions"] == []
    assert [(op.ticker, motif) for op, motif in signales] == [("AAA", "déjà revendu")]


def test_vente_d_une_autre_personne_ou_anterieure_n_empeche_pas_l_achat():
    client = FauxClient()
    ventes = [vente(operation=date(2026, 10, 10)), vente(personne="politicien:chambre:Autre|X|CA-02"),
              vente(ticker="ZZZ")]
    etat, _, signales = entrees(etat_vide(), [achat()], {"AAA": 100.0}, client, P, AUJOURD_HUI, ventes=ventes)
    assert len(client.appels_de("passer_oto")) == 1 and signales == []


def test_copier_passe_les_ventes_aux_entrees():
    client = FauxClient()
    messages, signales = copier([achat()], [vente(operation=date(2026, 10, 18))], {"AAA": 100.0}, client, P,
                                AUJOURD_HUI)
    assert [motif for _, motif in signales] == ["déjà revendu"] and client.appels_de("passer_oto") == []
