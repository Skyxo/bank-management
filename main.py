from flask import Flask, redirect, request, send_file, abort, session, render_template, after_this_request
import requests
import pandas as pd
import io
import os
import json
import tempfile
import time

CLIENT_ID     = "30487334"
CLIENT_SECRET = "sh8PkDfsTzbt9x5P88vEYGUV00Xu7tUz"
DOMAIN        = "cb2004lcl-sandbox.biapi.pro"
# Use localhost for local development
REDIRECT_URI  = "https://localhost:5000/callback"

app = Flask(__name__)
# Utiliser une clé stable (idéalement via variable d'environnement) pour éviter d'invalider la session entre les redirections
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
# Renforcer la fiabilité des cookies de session avec HTTPS
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

@app.route("/")
def start():
    # Optionnel: récupérer une période saisie par l'utilisateur via query params
    # Exemple: /?start_date=2024-01-01&end_date=2024-12-31
    sd = request.args.get("start_date")
    ed = request.args.get("end_date")
    full_history = request.args.get("full_history")
    if sd or ed:
        from datetime import datetime
        try:
            if sd:
                datetime.strptime(sd, "%Y-%m-%d")
            if ed:
                datetime.strptime(ed, "%Y-%m-%d")
            # Si une seule date est fournie, on laisse l'autre à None (sera gérée côté callback)
            # Si les deux sont fournies et dans le mauvais ordre, on swap
            if sd and ed and sd > ed:
                sd, ed = ed, sd
            session['date_from'] = sd
            session['date_to'] = ed
            print(f"📅 Période utilisateur enregistrée: from={sd} to={ed}")
        except ValueError:
            print("⚠️ start_date/end_date invalides, ignorées. Format attendu YYYY-MM-DD.")
            session.pop('date_from', None)
            session.pop('date_to', None)
    # Historique complet demandé
    if full_history is not None:
        session['full_history'] = str(full_history)
        print(f"🕰️ full_history activé: {session['full_history']}")

    # 1) Init user token
    resp = requests.post(
        f"https://{DOMAIN}/2.0/auth/init",
        json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    )
    data = resp.json()
    if resp.status_code != 200 or "auth_token" not in data:
        abort(500, f"Init error: {data}")
    # Store token in session instead of global variable
    session['user_token'] = data["auth_token"]

    # 2) Get temporary code
    code_resp = requests.get(
        f"https://{DOMAIN}/2.0/auth/token/code",
        headers={"Authorization": f"Bearer {session['user_token']}"}
    )
    code_data = code_resp.json()
    if code_resp.status_code != 200 or "code" not in code_data:
        abort(500, f"Code error: {code_data}")
    temp_code = code_data["code"]

    # 3) Redirect to Powens Webview
    url = (
        "https://webview.powens.com/connect"
        f"?domain={DOMAIN}"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&code={temp_code}"
    )
    return redirect(url)

@app.route("/callback")  # Separate route for callback
def callback():
    # Récupère connection_id (tu peux l'enregistrer si besoin)
    conn_id = request.args.get("connection_id")
    if not conn_id:
        abort(400, "Missing connection_id")
    # Sauvegarder systématiquement le connection_id pour usage ultérieur
    session['connection_id'] = conn_id
    
    # Si le token n'existe pas dans la session, réinitialiser l'authentification
    # et conserver le connection_id pour le réutiliser après
    if 'user_token' not in session:
        print("⚠️ Token de session perdu lors de la redirection, réinitialisation...")
        resp = requests.post(
            f"https://{DOMAIN}/2.0/auth/init",
            json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
        )
        data = resp.json()
        if resp.status_code != 200 or "auth_token" not in data:
            abort(500, f"Init error: {data}")
        session['user_token'] = data["auth_token"]
        print(f"✅ Nouveau token obtenu et connection_id {conn_id} sauvegardé")

    # 4) Liste des comptes
    accs_resp = requests.get(
        f"https://{DOMAIN}/2.0/users/me/accounts",
        headers={"Authorization": f"Bearer {session['user_token']}"}
    )

    if accs_resp.status_code != 200:
        abort(500, f"Accounts error: {accs_resp.json()}")

    accounts_data = accs_resp.json()

    # Debug : afficher la réponse brute
    print("\n--- Données brutes comptes ---")
    print(json.dumps(accounts_data, indent=2, ensure_ascii=False))

    # Déterminer la structure : list ou dict
    if isinstance(accounts_data, dict) and "accounts" in accounts_data:
        accounts = accounts_data["accounts"]
    elif isinstance(accounts_data, list):
        accounts = accounts_data
    else:
        abort(500, f"Unexpected accounts data format: {accounts_data}")

    # Debug : afficher chaque compte individuellement
    print(f"\n--- {len(accounts)} compte(s) trouvé(s) ---")
    for i, acc in enumerate(accounts):
        print(f"\n--- Compte #{i + 1} ---")
        print(json.dumps(acc, indent=2, ensure_ascii=False))


    # 5) Récupère toutes les transactions, pagination incluse
    all_transactions = []

    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        
        if "id" not in acc:
            print(f"⚠️ Compte sans ID détecté : {acc}")
            continue

        acc_id = acc["id"]
        acc_name = acc.get("name") or acc.get("original_name") or "Unknown Account"
        print(f"\n--- 🔍 Récupération des transactions pour le compte ID {acc_id} ({acc_name}) ---")

        # Période: priorité à celle fournie par l'utilisateur (session),
        # sinon si full_history demandé -> pas de filtre ou min_date très ancienne,
        # sinon fallback 10 ans
        from datetime import datetime, timedelta
        user_from = session.get('date_from')
        user_to = session.get('date_to')
        full_history_flag = str(session.get('full_history', '')).lower() in ("1", "true", "yes")
        min_date_str = None
        max_date_str = None
        use_dates = False
        if user_from or user_to:
            # Valider/compléter la période utilisateur
            try:
                if user_from:
                    datetime.strptime(user_from, "%Y-%m-%d")
                if user_to:
                    datetime.strptime(user_to, "%Y-%m-%d")
                # Si une seule borne: compléter raisonnablement
                if user_from and not user_to:
                    max_date_str = datetime.now().strftime("%Y-%m-%d")
                    min_date_str = user_from
                elif user_to and not user_from:
                    min_date_str = (datetime.now() - timedelta(days=365*10)).strftime("%Y-%m-%d")
                    max_date_str = user_to
                else:
                    # Les deux fournies
                    min_date_str = min(user_from, user_to)
                    max_date_str = max(user_from, user_to)
                use_dates = True
            except ValueError:
                print("⚠️ Période en session invalide, ignorée.")
                use_dates = False
        if not use_dates:
            if full_history_flag:
                # Historique complet: ne pas restreindre par dates, ou utiliser un min_date très ancien
                min_date_str = "1900-01-01"
                max_date_str = None
            else:
                # Fallback 10 ans
                today = datetime.now()
                ten_years_ago = today - timedelta(days=365*10)
                min_date_str = ten_years_ago.strftime("%Y-%m-%d")
                max_date_str = today.strftime("%Y-%m-%d")
        print(f"📅 Mode: {'full_history' if full_history_flag and not use_dates else 'periode utilisateur' if use_dates else 'fallback 10 ans'} | from={min_date_str} to={max_date_str}")
        
        # Essayer plusieurs endpoints API
        endpoints = [
            f"https://{DOMAIN}/2.0/users/me/accounts/{acc_id}/transactions",
            f"https://{DOMAIN}/2.0/users/me/transactions?id_account={acc_id}",
            f"https://{DOMAIN}/2.0/banks/accounts/{acc_id}/transactions"
        ]
        
        tx_found = False
        
        def try_fetch_once():
            nonlocal tx_found, all_transactions
            for endpoint_url in endpoints:
                if tx_found:
                    break
                    
                print(f"\n--- Essai avec endpoint: {endpoint_url} ---")
                offset, limit = 0, 500
                page = 1
                
                # Essayer différentes combinaisons de paramètres
                param_sets = []
                if full_history_flag and not use_dates:
                    # Historique complet: d'abord sans filtre de date, puis variantes min_date
                    param_sets.extend([
                        {"limit": limit, "offset": offset},
                        {"limit": limit, "offset": offset, "min_date": "1900-01-01"},
                        {"limit": limit, "offset": offset, "from": "1900-01-01"},
                        {"limit": limit, "offset": offset, "date_from": "1900-01-01"},
                    ])
                else:
                    # Période (utilisateur ou fallback)
                    base = {"limit": limit, "offset": offset}
                    param_sets.append({**base, "filter": "date"})
                    if min_date_str and max_date_str:
                        param_sets.append({**base, "filter": "date", "min_date": min_date_str, "max_date": max_date_str})
                        param_sets.append({**base, "from": min_date_str, "to": max_date_str})
                        param_sets.append({**base, "date_from": min_date_str, "date_to": max_date_str})
                    elif min_date_str and not max_date_str:
                        # Uniquement borne basse (full_history sans borne haute)
                        param_sets.append({**base, "min_date": min_date_str})
                        param_sets.append({**base, "from": min_date_str})
                        param_sets.append({**base, "date_from": min_date_str})
                
                for params in param_sets:
                    if tx_found:
                        break
                        
                    print(f"\nEssai avec paramètres: {params}")
                    tx_resp = requests.get(
                        endpoint_url,
                        headers={"Authorization": f"Bearer {session['user_token']}"},
                        params=params
                    )

                    if tx_resp.status_code != 200:
                        print(f"❌ Erreur API : {tx_resp.status_code} - {tx_resp.text}")
                        continue  # Essayer le prochain set de paramètres

                    data = tx_resp.json()
                    
                    # Debug : afficher le JSON brut de la réponse
                    print(f"📄 Page {page} – offset {offset}")
                    try:
                        print(json.dumps(data, indent=2, ensure_ascii=False)[:500] + "...")
                    except Exception:
                        print(str(data)[:500] + "...")

                    # Extraire les transactions selon différents formats possibles
                    txs = None
                    if isinstance(data, dict):
                        for key in ["transactions", "results", "data", "items"]:
                            if key in data:
                                txs = data[key]
                                break
                    elif isinstance(data, list):
                        txs = data
                    
                    if not txs:
                        print("⚠️ Aucune transaction trouvée dans cette réponse.")
                        continue  # Essayer avec d'autres paramètres
                    
                    if len(txs) > 0:
                        tx_found = True
                        print(f"✅ {len(txs)} transactions trouvées!")
                        
                        # Ajout des infos de compte à chaque transaction
                        for tx in txs:
                            tx["account_id"] = acc_id
                            tx["account_name"] = acc_name
                            
                            # Extraire des attributs imbriqués si nécessaire
                            if "attributes" in tx:
                                attrs = tx["attributes"]
                                for key, value in attrs.items():
                                    if key not in tx:  # Ne pas écraser les champs existants
                                        tx[key] = value
                        
                        all_transactions.extend(txs)
                        
                        # Continuer avec la pagination si nécessaire
                        while len(txs) == limit:
                            offset += limit
                            page += 1
                            params.update({"offset": offset})
                            
                            tx_resp = requests.get(
                                endpoint_url,
                                headers={"Authorization": f"Bearer {session['user_token']}"},
                                params=params
                            )
                            
                            if tx_resp.status_code != 200:
                                print(f"❌ Erreur API en pagination: {tx_resp.status_code}")
                                break
                                
                            data = tx_resp.json()
                            
                            # Extraire les transactions de la même manière
                            if isinstance(data, dict):
                                for key in ["transactions", "results", "data", "items"]:
                                    if key in data:
                                        txs = data[key]
                                        break
                            elif isinstance(data, list):
                                txs = data
                            else:
                                txs = []
                                
                            print(f"📄 Page {page} – offset {offset} : {len(txs)} transactions")
                            
                            if not txs:
                                break
                                
                            # Ajouter les nouvelles transactions
                            for tx in txs:
                                tx["account_id"] = acc_id
                                tx["account_name"] = acc_name
                                
                                if "attributes" in tx:
                                    attrs = tx["attributes"]
                                    for key, value in attrs.items():
                                        if key not in tx:
                                            tx[key] = value
                                            
                            all_transactions.extend(txs)
                            
                            if len(txs) < limit:
                                print("✅ Dernière page atteinte.")
                                break

        # 1ère tentative
        try_fetch_once()

        # Si rien trouvé, tenter un rafraîchissement de la connexion puis retenter rapidement
        if not tx_found:
            connection_id = acc.get("id_connection") or session.get('connection_id')
            if connection_id:
                refresh_url = f"https://{DOMAIN}/2.0/users/me/connections/{connection_id}/refresh"
                try:
                    print(f"🔄 Tentative de refresh de la connexion {connection_id}...")
                    refresh_resp = requests.put(
                        refresh_url,
                        headers={"Authorization": f"Bearer {session['user_token']}"}
                    )
                    print(f"Réponse refresh: {refresh_resp.status_code} - {refresh_resp.text[:200]}")
                except Exception as e:
                    print(f"Erreur lors du refresh: {e}")
                # Attendre brièvement puis retenter une fois
                time.sleep(3)
                try_fetch_once()

        if not tx_found:
            print(f"⚠️ Aucune transaction trouvée pour le compte {acc_name} après plusieurs tentatives.")

    print(f"\n✅ Total transactions récupérées : {len(all_transactions)}")

    # 6) Create DataFrame
    df = pd.DataFrame(all_transactions)
    
    # Display columns for debugging
    print("DataFrame columns:", df.columns.tolist())

    # Garde seulement les colonnes utiles si elles existent
    cols = [
        "id",                   # ID de la transaction
        "id_account",           # ID du compte lié
        "account_name",         # Nom du compte
        "application_date",     # date "PFM" modifiable
        "date",                 # date d'émission en banque
        "value",                # montant net
        "original_value",       # montant dans devise originale (s'il y en a)
        "original_currency",    # devise originale
        "type",                 # type de transaction
        "original_wording",     # libellé complet de la banque
        "simplified_wording",   # libellé simplifié Powens
        "wording",              # libellé modifiable par l'utilisateur
        "categories",           # liste des catégories
        "comment",              # commentaire utilisateur
    ]
    
    # S'assurer que "amount" existe (copier depuis "value" si nécessaire)
    if "amount" not in df.columns and "value" in df.columns:
        df["amount"] = df["value"]
        cols.append("amount")    # Ajouter amount à la liste des colonnes
    
    existing_cols = [c for c in cols if c in df.columns]
    if existing_cols:
        df = df[existing_cols]

    # Sort by date (newest first)
    if "date" in df.columns:
        df = df.sort_values("date", ascending=False)
        
    # Conversion des dates en format datetime
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
    
    # Calcul des statistiques
    stats = {}
    amount_col = None
    if "value" in df.columns or "amount" in df.columns:
        amount_col = "amount" if "amount" in df.columns else "value"
        
        # Statistiques générales
        stats["solde"] = df[amount_col].sum()
        stats["total_depenses"] = abs(df[df[amount_col] < 0][amount_col].sum())
        stats["total_revenus"] = df[df[amount_col] > 0][amount_col].sum()
        
        # Statistiques par mois (3 derniers mois)
        if "date" in df.columns:
            df["mois"] = df["date"].dt.strftime("%Y-%m")
            mois_stats = df.groupby("mois").agg({
                amount_col: ["sum", "count", 
                            lambda x: x[x > 0].sum(),  # revenus
                            lambda x: abs(x[x < 0].sum())  # dépenses
                           ]
            })
            mois_stats.columns = ["total", "nb_transactions", "revenus", "depenses"]
            stats["par_mois"] = mois_stats.sort_index(ascending=False).head(3).to_dict("index")
        
        # Statistiques par type de transaction
        if "type" in df.columns:
            type_stats = df.groupby("type").agg({
                amount_col: ["count", "mean", "sum"]
            })
            type_stats.columns = ["nb_transactions", "montant_moyen", "montant_total"]
            stats["par_type"] = type_stats.to_dict("index")
        
        # Statistiques par catégorie (si disponible)
        if "categories" in df.columns:
            # Les catégories peuvent être un champ complexe, simplification
            df["categorie_principale"] = df["categories"].apply(
                lambda x: x[0]["code"] if isinstance(x, list) and len(x) > 0 and isinstance(x[0], dict) and "code" in x[0] 
                else "non_categorise"
            )
            cat_stats = df.groupby("categorie_principale").agg({
                amount_col: ["count", "sum"]
            })
            cat_stats.columns = ["nb_transactions", "montant_total"]
            stats["par_categorie"] = cat_stats.to_dict("index")

    # Préparer les données de graphiques (dépenses journalières et solde cumulé)
    chart_data = {"expenses": None, "balance": None}
    try:
        if "date" in df.columns and amount_col:
            # Agréger par jour (tri ascendant)
            daily_sum = (
                df.sort_values("date")
                  .groupby(df["date"].dt.date)[amount_col]
                  .sum()
            )
            # Dépenses: valeurs négatives en absolu, sinon 0
            daily_expenses = daily_sum.apply(lambda v: float(-v) if v < 0 else 0.0)
            # Solde cumulé des flux
            cumulative_balance = daily_sum.cumsum().astype(float)
            # Sérialisation
            chart_data["expenses"] = {
                "labels": [d.strftime("%Y-%m-%d") for d in daily_expenses.index],
                "values": daily_expenses.tolist(),
            }
            chart_data["balance"] = {
                "labels": [d.strftime("%Y-%m-%d") for d in cumulative_balance.index],
                "values": cumulative_balance.tolist(),
            }

            # Dépenses par centres d'intérêt (heuristique par mots-clés)
            interest_definitions = {
                "Fast food": ["mcdo", "mcdonald", "burger king", "bk ", "kfc", "quick", "five guys", "subway", "domino", "pizza hut", "ubereats", "uber eats", "deliveroo", "just eat", "kebab", "taco bell"],
                "Internet": ["free mobile", "red by sfr", "sosh", "freebox", "free ", "sfr", "bouygues", "b&you", "orange", "numericable", "fibre", "box", "internet"],
                "Abonnements": ["spotify", "netflix", "youtube premium", "youtube music", "prime video", "amazon prime", "disney", "canal+", "canal plus", "deezer", "icloud", "google one", "onedrive", "microsoft 365", "adobe"],
                "Supermarché": ["carrefour", "auchan", "leclerc", "e.leclerc", "intermarche", "intermarché", "lidl", "aldi", "casino", "monoprix", "franprix", "simply", "u express", "hyper u", "super u"],
                "Restaurants": ["restaurant", "bistrot", "bistro", "brasserie", "cafe ", "café", "bar ", "coffee", "starbucks", "paul ", "pret a manger", "pizza ", "sushi"],
                "Transport": ["sncf", "ouigo", "ratp", "tcl ", "rtm ", "tan ", "tbm ", "ter ", "uber ", "bolt ", "free now", "air france", "ryanair", "transavia", "easyjet", "blablacar", "total", "esso", "station service", "autoroute", "péage", "peage"],
                "Logement": ["loyer", "rent", "airbnb", "booking", "edf", "enedis", "engie", "gdf", "gaz", "eau ", "veolia", "saur ", "suez"],
                "Santé": ["pharmacie", "pharmacy", "doctolib", "mutuelle", "santé", "dentiste", "opticien"],
                "Loisirs": ["fnac", "micromania", "steam", "epic games", "playstation", "xbox", "nintendo", "cinema", "gaumont", "ugc", "kinepolis", "concert", "billet", "ticketmaster"],
                "Frais bancaires": ["frais ", "commission", "agios", "tenue de compte", "cotisation", "frais bancaires"]
            }
            interest_colors = {
                "Fast food": {"border": "#e74c3c", "bg": "rgba(231,76,60,0.15)"},
                "Internet": {"border": "#8e44ad", "bg": "rgba(142,68,173,0.15)"},
                "Abonnements": {"border": "#16a085", "bg": "rgba(22,160,133,0.15)"},
                "Supermarché": {"border": "#27ae60", "bg": "rgba(39,174,96,0.15)"},
                "Restaurants": {"border": "#d35400", "bg": "rgba(211,84,0,0.15)"},
                "Transport": {"border": "#2980b9", "bg": "rgba(41,128,185,0.15)"},
                "Logement": {"border": "#2c3e50", "bg": "rgba(44,62,80,0.15)"},
                "Santé": {"border": "#2ecc71", "bg": "rgba(46,204,113,0.15)"},
                "Loisirs": {"border": "#f1c40f", "bg": "rgba(241,196,15,0.15)"},
                "Frais bancaires": {"border": "#7f8c8d", "bg": "rgba(127,140,141,0.15)"},
                "Autres": {"border": "#95a5a6", "bg": "rgba(149,165,166,0.15)"}
            }

            def detect_interest_center(row) -> str:
                text_fields = []
                for f in ("simplified_wording", "original_wording", "wording"):
                    if f in row and isinstance(row[f], str):
                        text_fields.append(row[f])
                text = (" ".join(text_fields)).lower()
                # Catégorie Powens si dispo
                try:
                    if isinstance(row.get("categories"), list) and len(row["categories"]) > 0:
                        code = str(row["categories"][0].get("code", "")).lower()
                        text += f" {code}"
                except Exception:
                    pass
                # Matching par priorité
                for center, keywords in interest_definitions.items():
                    for kw in keywords:
                        if kw in text:
                            return center
                return "Autres"

            df_interest = df.copy()
            # Ne considérer que les dépenses
            df_interest = df_interest[df_interest[amount_col] < 0]
            if not df_interest.empty:
                df_interest["interest_center"] = df_interest.apply(detect_interest_center, axis=1)
                # Index de dates complet pour lisser les trous
                date_index = pd.date_range(start=df_interest["date"].min().normalize(), end=df_interest["date"].max().normalize(), freq="D")
                pivot = (
                    df_interest.assign(date_only=df_interest["date"].dt.normalize())
                               .groupby(["date_only", "interest_center"])[amount_col]
                               .sum()
                               .unstack(fill_value=0.0)
                               .reindex(date_index, fill_value=0.0)
                )
                # Transformer en dépenses positives
                pivot = (-pivot).clip(lower=0.0)
                labels = [d.strftime("%Y-%m-%d") for d in pivot.index]
                datasets = []
                for center in pivot.columns:
                    color = interest_colors.get(center, interest_colors["Autres"])
                    datasets.append({
                        "label": center,
                        "values": [float(v) for v in pivot[center].tolist()],
                        "borderColor": color["border"],
                        "backgroundColor": color["bg"]
                    })
                chart_data["by_interest"] = {
                    "labels": labels,
                    "datasets": datasets
                }
    except Exception as e:
        print(f"⚠️ Erreur préparation chart_data: {e}")

    # 7) Save CSV data for download button (côté serveur pour éviter un cookie trop gros)
    buffer = io.StringIO()
    df.to_csv(buffer, index=False, encoding="utf-8-sig")
    buffer.seek(0)

    tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.csv')
    try:
        tmp_file.write(buffer.getvalue().encode('utf-8'))
        temp_file_path = tmp_file.name
    finally:
        tmp_file.close()
    # Stocker uniquement le chemin du fichier dans la session (léger)
    session['csv_temp_path'] = temp_file_path

    # Render the index.html template
    return render_template(
        "index.html",
        transactions=df,
        columns=existing_cols if existing_cols else [],
        stats=stats,
        chart_data=chart_data
    )

@app.route("/download")
def download_csv():
    if 'csv_temp_path' not in session:
        return redirect('/')
    
    csv_path = session['csv_temp_path']
    if not os.path.exists(csv_path):
        return redirect('/')

    # Supprimer le fichier temporaire après l'envoi de la réponse
    @after_this_request
    def remove_file(response):
        try:
            os.remove(csv_path)
        except Exception:
            pass
        return response
        
    return send_file(
        csv_path,
        mimetype="text/csv",
        as_attachment=True,
        download_name="releves_complets.csv"
    )

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000, ssl_context="adhoc")