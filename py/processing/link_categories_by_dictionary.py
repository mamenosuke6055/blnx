import sqlite3
import pandas as pd
from pathlib import Path
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Define project base directory
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Define database paths
FINANCE_DB_PATH = BASE_DIR / "db" / "finance.db"
DICTIONARY_DB_PATH = BASE_DIR / "db" / "dictionary.db"

def get_db_connection(db_path):
    """Establishes a connection to the SQLite database."""
    try:
        conn = sqlite3.connect(db_path)
        return conn
    except sqlite3.Error as e:
        logging.error(f"Error connecting to database {db_path}: {e}")
        return None

def load_category_dictionary(conn):
    """Loads the category mapping from the dictionary database."""
    query = """
    SELECT
        cd.description,
        cdm.main_category,
        cdm.sub_category
    FROM category_dict AS cd
    JOIN category_dict_manual AS cdm ON cd.category_id = cdm.id
    """
    try:
        df = pd.read_sql_query(query, conn)
        logging.info(f"Loaded {len(df)} category rules from dictionary.")
        return df
    except Exception as e:
        logging.error(f"Failed to load category dictionary: {e}")
        return pd.DataFrame()

def load_accounts(conn):
    """Loads accounts from the finance database into a DataFrame."""
    query = "SELECT guid, name, account_type, parent_guid FROM accounts"
    try:
        df = pd.read_sql_query(query, conn)
        logging.info(f"Loaded {len(df)} accounts from finance.db.")
        return df
    except Exception as e:
        logging.error(f"Failed to load accounts: {e}")
        return pd.DataFrame()

def find_account_guid(accounts_df, name, account_type, parent_guid=None):
    """Finds an existing account GUID from the accounts DataFrame."""
    candidates = accounts_df[(accounts_df['name'] == name) & (accounts_df['account_type'] == account_type)]
    if candidates.empty:
        return None
    if parent_guid:
        candidates = candidates[candidates['parent_guid'] == parent_guid]
    if not candidates.empty:
        return candidates.iloc[0]['guid']
    return None


def get_unclassified_transactions(conn):
    """
    Retrieves transactions that are currently linked to an 'Unclassified' expense or income account.
    """
    query = """
    SELECT
        t.guid as transaction_guid,
        t.description,
        s.guid as split_guid,
        s.account_guid as unclassified_account_guid,
        a.account_type as current_account_type
    FROM transactions AS t
    JOIN splits AS s ON t.guid = s.tx_guid
    JOIN accounts AS a ON s.account_guid = a.guid
    WHERE a.name = 'Uncategorized' AND a.account_type IN ('EXPENSE', 'INCOME')
    """
    try:
        df = pd.read_sql_query(query, conn)
        logging.info(f"Found {len(df)} unclassified transaction splits.")
        return df
    except Exception as e:
        logging.error(f"Failed to get unclassified transactions: {e}")
        return pd.DataFrame()


def categorize_and_update_transactions():
    """
    Main function to categorize transactions in finance.db using the dictionary.
    """
    logging.info("Starting transaction categorization process...")

    dict_conn = get_db_connection(DICTIONARY_DB_PATH)
    finance_conn = get_db_connection(FINANCE_DB_PATH)

    if not dict_conn or not finance_conn:
        logging.error("Could not establish database connections. Aborting.")
        return

    try:
        # 1. Load data
        category_rules_df = load_category_dictionary(dict_conn)
        accounts_df = load_accounts(finance_conn)
        unclassified_df = get_unclassified_transactions(finance_conn)

        if category_rules_df.empty or unclassified_df.empty:
            logging.info("No rules to apply or no unclassified transactions found. Exiting.")
            return

        # Create a lookup dictionary for faster matching
        category_lookup = {row['description'].lower(): (row['main_category'], row['sub_category']) for _, row in category_rules_df.iterrows()}

        # Get Root Account GUIDs
        expense_parent_guid_series = accounts_df[accounts_df['name'] == 'Expenses']['guid']
        income_parent_guid_series = accounts_df[accounts_df['name'] == 'Income']['guid']
        
        if expense_parent_guid_series.empty or income_parent_guid_series.empty:
            logging.error("Could not find 'Expenses' or 'Income' root account. Aborting.")
            return
            
        expense_parent_guid = expense_parent_guid_series.iloc[0]
        income_parent_guid = income_parent_guid_series.iloc[0]


        # 2. Categorize transactions
        updates_to_perform = []
        for _, unclassified_row in unclassified_df.iterrows():
            description = unclassified_row['description']
            
            matched_categories = category_lookup.get(description.lower())

            if matched_categories:
                main_cat_name, sub_cat_name = matched_categories

                # Determine if Income or Expense
                is_income = "収入" in main_cat_name
                target_type = 'INCOME' if is_income else 'EXPENSE'
                parent_root_guid = income_parent_guid if is_income else expense_parent_guid

                # Find main category account
                main_cat_guid = find_account_guid(accounts_df, main_cat_name, target_type, parent_root_guid)
                if not main_cat_guid:
                    logging.warning(f"Main category account '{main_cat_name}' ({target_type}) not found for description '{description}'. Skipping.")
                    continue

                # Find sub category account
                target_account_guid = find_account_guid(accounts_df, sub_cat_name, target_type, main_cat_guid)
                if not target_account_guid:
                    logging.warning(f"Sub category account '{sub_cat_name}' under '{main_cat_name}' not found for description '{description}'. Skipping.")
                    continue
                
                updates_to_perform.append({
                    'split_guid': unclassified_row['split_guid'],
                    'new_account_guid': target_account_guid,
                    'description': description,
                    'category': f"{main_cat_name}:{sub_cat_name}"
                })

        # 3. Update database
        if updates_to_perform:
            logging.info(f"Found {len(updates_to_perform)} transactions to update.")
            cursor = finance_conn.cursor()
            for update in updates_to_perform:
                try:
                    cursor.execute(
                        "UPDATE splits SET account_guid = ? WHERE guid = ?",
                        (update['new_account_guid'], update['split_guid'])
                    )
                    logging.info(f"Updated transaction '{update['description']}' to category '{update['category']}'")
                except sqlite3.Error as e:
                    logging.error(f"Failed to update split {update['split_guid']}: {e}")
                    finance_conn.rollback()
            finance_conn.commit()
            logging.info("Database update complete.")
        else:
            logging.info("No transactions were updated.")

    finally:
        if dict_conn:
            dict_conn.close()
        if finance_conn:
            finance_conn.close()
        logging.info("Process finished.")


if __name__ == "__main__":
    categorize_and_update_transactions()
