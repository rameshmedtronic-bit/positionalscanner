import streamlit as st
import pandas as pd
import requests
import datetime
import time
import os
import gzip
import json

import concurrent.futures

# --- Helpers ---
def get_ist_now():
    """Get current time in IST (UTC+5:30)"""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)

# --- Configuration ---
st.set_page_config(page_title="Intraday Option Scanner", layout="wide")

# Client View Toggle (Hide Sidebar)
client_view = st.checkbox("Enable Client View (Full Page)", value=False)

if client_view:
    st.markdown(
        """
        <style>
            [data-testid="stSidebar"] {display: none;}
            [data-testid="collapsedControl"] {display: none;}
        </style>
        """,
        unsafe_allow_html=True,
    )

# Update time for header
if not client_view:
    update_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
    st.markdown(f"""
        <div style='display: flex; justify-content: space-between; align-items: center; margin-top: -20px; margin-bottom: 10px;'>
            <h3 style='margin: 0;'>Intraday Option Scanner</h3>
            <span style='font-size: 1rem; color: #555;'>Last Updated: {update_time} (IST)</span>
        </div>
    """, unsafe_allow_html=True)

# --- Sidebar Inputs ---
st.sidebar.header("Configuration")

# --- Token Management ---
TOKEN_FILE = ".token_cache"

def get_today_str():
    """Get today's date in IST string format"""
    return str(get_ist_now().date())

def load_cached_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)
                if data.get("date") == get_today_str():
                    return data.get("token")
        except:
            pass
    return None

def save_token_to_cache(token):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"token": token, "date": get_today_str()}, f)

# Input for Access Token (Frontend Only)
cached_token = load_cached_token()
access_token = st.sidebar.text_input("Enter Access Token", type="password", value=cached_token if cached_token else "")

if access_token:
    # If user entered a new token, save it
    if access_token != cached_token:
        save_token_to_cache(access_token)
        st.sidebar.success("✅ New Token Saved for Today")
    else:
        st.sidebar.success("✅ Token Loaded from Cache (Valid for Today)")
else:
    st.warning("Please enter your Access Token in the sidebar to proceed.")
    st.stop()

# --- Instruments Data Synchronization ---
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
INSTRUMENTS_FILE = 'NSE.json'
CACHE_FILE = 'instruments_cache.pkl'

def is_file_fresh(filepath):
    """Check if file exists and is from today"""
    if not os.path.exists(filepath):
        return False
    try:
        file_time = datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
        return file_time.date() == datetime.date.today()
    except:
        return False

def download_and_extract_instruments():
    """Download and unzip instruments file to NSE.json"""
    status_placeholder = st.empty()
    status_placeholder.info("Downloading latest NSE.json from Upstox...")
    
    try:
        # Download with timeout and potential SSL handling
        try:
            response = requests.get(INSTRUMENTS_URL, stream=True, timeout=30)
            response.raise_for_status()
        except requests.exceptions.SSLError:
            status_placeholder.warning("SSL Certificate verification failed. Retrying without verification (not recommended but may work)...")
            response = requests.get(INSTRUMENTS_URL, stream=True, timeout=30, verify=False)
            response.raise_for_status()
        
        status_placeholder.info("Extracting NSE.json...")
        
        # Decompress and Save to File
        with gzip.GzipFile(fileobj=response.raw) as gz:
            with open(INSTRUMENTS_FILE, 'wb') as f_out:
                while True:
                    chunk = gz.read(1024*1024) # Read in chunks
                    if not chunk:
                        break
                    f_out.write(chunk)
                    
        status_placeholder.success("NSE.json updated successfully!")
        time.sleep(1)
        status_placeholder.empty()
        return True
        
    except Exception as e:
        status_placeholder.error(f"Failed to update instruments: {e}")
        return False

# --- Data Loading ---
@st.cache_data(ttl=3600*4, show_spinner=True)  # Cache for 4 hours
def load_data():
    df = None
    
    # 1. Try to load from fast pickle cache first
    if is_file_fresh(CACHE_FILE):
        try:
            df = pd.read_pickle(CACHE_FILE)
            # st.info("Loaded instruments from cache.")
        except Exception as e:
            st.warning(f"Could not load pickle cache: {e}")
            df = None

    # 2. If no cache, load from raw JSON
    if df is None:
        # Check freshness and download if needed
        if not is_file_fresh(INSTRUMENTS_FILE):
            if not download_and_extract_instruments():
                 # If download failed, try to use existing file
                 if not os.path.exists(INSTRUMENTS_FILE):
                     st.error("NSE.json missing and download failed.")
                     return pd.DataFrame(), pd.DataFrame()
                 else:
                     st.warning("Download failed, using existing (possibly outdated) NSE.json")

        # Load and Filter NSE.json directly
        try:
            if not os.path.exists(INSTRUMENTS_FILE):
                st.error(f"Critical Error: {INSTRUMENTS_FILE} does not exist.")
                return pd.DataFrame(), pd.DataFrame()
                
            with open(INSTRUMENTS_FILE, 'r') as f:
                data = json.load(f)
                
            if not isinstance(data, list):
                st.error(f"Unexpected data format in {INSTRUMENTS_FILE}. Expected a list.")
                return pd.DataFrame(), pd.DataFrame()

            # Filter list before creating DataFrame
            filtered_data = [
                row for row in data 
                if row.get('segment') == 'NSE_FO' and row.get('asset_type') in ['EQUITY', 'INDEX']
            ]
            
            del data # Free huge memory immediately

            if not filtered_data:
                 st.error("No relevant instruments found after filtering NSE.json (NSE_FO + EQUITY/INDEX).")
                 return pd.DataFrame(), pd.DataFrame()

            # Convert to DataFrame
            df = pd.DataFrame(filtered_data)
            del filtered_data # Free list memory
            
            # Save to fast cache for next run
            try:
                df.to_pickle(CACHE_FILE)
            except Exception as e:
                st.warning(f"Failed to save pickle cache: {e}")
            
        except Exception as e:
            st.error(f"Error loading NSE.json: {e}")
            import traceback
            st.code(traceback.format_exc())
            return pd.DataFrame(), pd.DataFrame()

    # --- Process DataFrames ---
    
    # 1. Options DF (All NSE_FO EQUITY/INDEX)
    options_df = df.copy()
    
    # 2. Futures DF (Current Month FUT)
    df_fut = df[df['instrument_type'].str.contains('FUT', na=False)].copy()
    
    # Filter for Near Month Expiry (Nearest valid expiry >= Today)
    if 'expiry' in df_fut.columns:
        # Convert expiry from milliseconds to datetime for filtering
        df_fut['expiry_dt'] = pd.to_datetime(df_fut['expiry'], unit='ms')
        
        # Use IST date for comparison
        current_date = get_ist_now().date()
        
        # Filter futures that haven't expired yet
        active_futures = df_fut[df_fut['expiry_dt'].dt.date >= current_date]
        
        if not active_futures.empty:
            # Find the nearest expiry date across ALL active futures
            nearest_expiry = active_futures['expiry_dt'].min()
            
            # Filter only futures matching this nearest expiry
            df_fut = active_futures[active_futures['expiry_dt'] == nearest_expiry]
        else:
            df_fut = pd.DataFrame()

        # Drop temp column
        if not df_fut.empty:
            df_fut = df_fut.drop(columns=['expiry_dt'])
        
    futures_df = df_fut
        
    # Convert expiry to datetime
    if 'expiry' in futures_df.columns:
        futures_df['expiry_date'] = pd.to_datetime(futures_df['expiry'], unit='ms')
    if 'expiry' in options_df.columns:
        options_df['expiry_date'] = pd.to_datetime(options_df['expiry'], unit='ms')
    
    return futures_df, options_df

# --- Main App Logic ---

# Initialize data with spinner
with st.spinner("Initializing Application and Loading Data..."):
    futures_df, options_df = load_data()

if futures_df.empty or options_df.empty:
    st.error("Failed to load instruments data. Please check your internet connection and restart.")
    if st.button("Clear Cache & Retry"):
        st.cache_data.clear()
        # Also try to delete the local files if they are suspected to be corrupt
        for f in [INSTRUMENTS_FILE, CACHE_FILE]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass
        st.rerun()
    st.stop()

# --- API Functions ---
def get_ohlc(instrument_key, token):
    url = "https://api.upstox.com/v3/market-quote/ohlc"
    params = {
        'instrument_key': instrument_key,
        'interval': '1d'
    }
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache'
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data['status'] == 'success':
            return data['data']
    except Exception as e:
        # st.error(f"Error fetching OHLC: {e}") # Suppress individual errors for batch
        pass
    return {}

def get_ltp(instrument_keys, token):
    url = "https://api.upstox.com/v3/market-quote/ltp"
    params = {'instrument_key': instrument_keys}
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}',
        'Cache-Control': 'no-cache'
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data['status'] == 'success':
            return data['data']
    except Exception as e:
        st.error(f"Error fetching LTP: {e}")
        print(f"LTP Fetch Error: {e}")
        pass
    return {}

# Helper function to find data by token
def find_data_by_token(token, data_dict):
    if not data_dict: return None
    # 1. Try direct key lookup
    if token in data_dict: return data_dict[token]
    if token.replace('|', ':') in data_dict: return data_dict[token.replace('|', ':')]
    
    # 2. Iterate and match instrument_token field
    for key, value in data_dict.items():
        if value.get('instrument_token') == token:
            return value
    return None

# --- Main Logic ---

# Auto-Refresh Controls
st.sidebar.markdown("---")
st.sidebar.header("Auto-Refresh Settings")
atm_mode = st.sidebar.radio("ATM Strike Based On:", ("Fixed (Open Price)", "Dynamic (LTP)"), index=0)
auto_refresh = st.sidebar.checkbox("Enable Auto-Refresh", value=False)
refresh_interval = st.sidebar.number_input("Refresh Interval (seconds)", min_value=5, value=30, step=5)

# Determine if we should run
run_once = False
if not client_view:
    run_once = st.button("🔄 Refresh Data", type="primary")
    
should_run = run_once or auto_refresh

# Define column name globally based on selection
price_key = 'open' if atm_mode == "Fixed (Open Price)" else 'close'
future_col_name = "Future Open" if atm_mode == "Fixed (Open Price)" else "Future LTP"

if should_run:
    # --- Time Restriction Check (09:00 AM - 03:40 PM IST) ---
    ist_now = get_ist_now()
    market_start = ist_now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_end = ist_now.replace(hour=15, minute=40, second=0, microsecond=0)
    
    # Check if current time is within trading hours
    is_market_closed = not (market_start <= ist_now <= market_end)
    
    if is_market_closed:
        if not client_view:
            st.warning(f"⚠️ Market Closed ({ist_now.strftime('%H:%M:%S')} IST). Auto-refresh is disabled. Showing final data.")
        # Proceed to fetch data once so the user can see the last state.
    
    if auto_refresh:
        if not client_view:
            st.caption(f"Auto-refreshing every {refresh_interval} seconds...")
        
    # --- Silent Update Logic ---
    # We want to avoid 'shaking' which is caused by the spinner and progress bars appearing/disappearing.
    # If client_view is ON, we suppress the spinner and progress bars.
    
    if client_view:
        # No spinner, no progress bar
        # Just run the logic directly. The user won't see a loading state, but the table will just update.
        # This mimics the "silent update" behavior.
        futures_df_sorted = futures_df.sort_values('expiry_date')
        unique_futures = futures_df_sorted.drop_duplicates(subset=['name'], keep='first')
        
        all_results = []
        
        # Batch processing for Futures OHLC
        chunk_size = 20
        future_records = unique_futures.to_dict('records')
        total_records = len(future_records)
        
        # No visible progress bar for client view
        progress_bar = None
        status_text = None
        fetch_errors = []
        
        # Helper to calculate percentage change
        def calc_pct_change(ltp, cp):
            if ltp is not None and cp and cp > 0:
                return ((ltp - cp) / cp) * 100
            return 0.0

        # We need to map Future Prices first
        future_prices = {} # {symbol_name: price}
        
        # Prepare chunks
        chunks = [future_records[i:i+chunk_size] for i in range(0, total_records, chunk_size)]
        total_chunks = len(chunks)
        
        # Function to process a single chunk of futures
        def fetch_futures_chunk(chunk):
            keys = ",".join([r['instrument_key'] for r in chunk])
            ohlc_data = get_ohlc(keys, access_token)
            results = {}
            if ohlc_data:
                # Normalize keys
                lookup_map = {}
                for k, v in ohlc_data.items():
                    lookup_map[k] = v
                    if 'instrument_token' in v:
                        lookup_map[v['instrument_token']] = v
                
                for record in chunk:
                    key = record['instrument_key']
                    data = lookup_map.get(key)
                    if not data:
                        alt_key = key.replace('|', ':')
                        data = lookup_map.get(alt_key)
                    
                    if data:
                        ohlc_source = data.get('live_ohlc') or data.get('prev_ohlc')
                        if ohlc_source:
                            results[record['name']] = ohlc_source.get(price_key, 0.0)
            return results

        # Parallel Execution for Futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_chunk = {executor.submit(fetch_futures_chunk, chunk): i for i, chunk in enumerate(chunks)}
            
            for future in concurrent.futures.as_completed(future_to_chunk):
                try:
                    chunk_results = future.result()
                    future_prices.update(chunk_results)
                except Exception as e:
                    fetch_errors.append(f"Futures Fetch Error: {str(e)}")

        # Prepare Option Keys to Fetch
        option_keys_to_fetch = []
        symbol_atm_map = {} # {symbol: {atm_strike, ce_key, pe_key}}
        
        relevant_options_df = options_df[
            (options_df['instrument_type'].isin(['CE', 'PE'])) &
            (options_df['name'].isin(future_prices.keys()))
        ]
        
        if not relevant_options_df.empty:
             relevant_options_df['expiry_date_obj'] = relevant_options_df['expiry_date'].dt.date
        
        options_grouped = relevant_options_df.groupby('name')
        
        for i, (symbol, ref_price) in enumerate(future_prices.items()):
            if ref_price <= 0: continue
            
            if symbol not in options_grouped.groups:
                continue
                
            opts_group = options_grouped.get_group(symbol)
            
            f_rec = unique_futures[unique_futures['name'] == symbol].iloc[0]
            f_expiry = f_rec['expiry_date'].date()
            
            # Extract short symbol for display
            short_symbol = f_rec.get('asset_symbol') or f_rec.get('underlying_symbol') or symbol
            
            opts = opts_group[opts_group['expiry_date_obj'] == f_expiry]
            
            if opts.empty: continue
            
            unique_strikes = sorted(opts['strike_price'].unique())
            if not unique_strikes: continue
            
            atm_strike = min(unique_strikes, key=lambda x: abs(x - ref_price))
            
            ce_row = opts[(opts['strike_price'] == atm_strike) & (opts['instrument_type'] == 'CE')]
            pe_row = opts[(opts['strike_price'] == atm_strike) & (opts['instrument_type'] == 'PE')]
            
            ce_key = ce_row.iloc[0]['instrument_key'] if not ce_row.empty else None
            pe_key = pe_row.iloc[0]['instrument_key'] if not pe_row.empty else None
            
            ce_lot = ce_row.iloc[0]['lot_size'] if not ce_row.empty else 0
            pe_lot = pe_row.iloc[0]['lot_size'] if not pe_row.empty else 0
            
            symbol_atm_map[symbol] = {
                'ref_price': ref_price,
                'atm_strike': atm_strike,
                'ce_key': ce_key,
                'pe_key': pe_key,
                'ce_lot': ce_lot,
                'pe_lot': pe_lot,
                'display_symbol': short_symbol
            }
            
            if ce_key: option_keys_to_fetch.append(ce_key)
            if pe_key: option_keys_to_fetch.append(pe_key)
            
        # Batch Fetch Options Data
        options_data_map = {}
        total_opt_keys = len(option_keys_to_fetch)
        
        def fetch_options_chunk(chunk_keys):
            keys_str = ",".join(chunk_keys)
            return get_ltp(keys_str, access_token)

        if total_opt_keys > 0:
            opt_chunks = [option_keys_to_fetch[i:i+chunk_size] for i in range(0, total_opt_keys, chunk_size)]
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_to_chunk = {executor.submit(fetch_options_chunk, chunk): i for i, chunk in enumerate(opt_chunks)}
                
                for future in concurrent.futures.as_completed(future_to_chunk):
                    try:
                        ltp_data = future.result()
                        if ltp_data:
                            options_data_map.update(ltp_data)
                    except Exception as e:
                        fetch_errors.append(f"Options Fetch Error: {str(e)}")
        
        if fetch_errors:
            st.error(f"Errors occurred during data fetch ({len(fetch_errors)}). Data might be incomplete.")
            with st.expander("View Errors"):
                for err in fetch_errors:
                    st.write(err)
        
    else:
        # Standard View with Spinner and Progress
        with st.spinner("Fetching and Calculating Data..."):
            futures_df_sorted = futures_df.sort_values('expiry_date')
            unique_futures = futures_df_sorted.drop_duplicates(subset=['name'], keep='first')
            
            all_results = []
            
            # Batch processing for Futures OHLC
            chunk_size = 20
            future_records = unique_futures.to_dict('records')
            total_records = len(future_records)
            
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            # Helper to calculate percentage change
            def calc_pct_change(ltp, cp):
                if ltp is not None and cp and cp > 0:
                    return ((ltp - cp) / cp) * 100
                return 0.0

            # We need to map Future Prices first
            future_prices = {} # {symbol_name: price}
            
            chunks = [future_records[i:i+chunk_size] for i in range(0, total_records, chunk_size)]
            total_chunks = len(chunks)
            
            def fetch_futures_chunk(chunk):
                keys = ",".join([r['instrument_key'] for r in chunk])
                ohlc_data = get_ohlc(keys, access_token)
                results = {}
                if ohlc_data:
                    lookup_map = {}
                    for k, v in ohlc_data.items():
                        lookup_map[k] = v
                        if 'instrument_token' in v:
                            lookup_map[v['instrument_token']] = v
                    
                    for record in chunk:
                        key = record['instrument_key']
                        data = lookup_map.get(key)
                        if not data:
                            alt_key = key.replace('|', ':')
                            data = lookup_map.get(alt_key)
                        
                        if data:
                            ohlc_source = data.get('live_ohlc') or data.get('prev_ohlc')
                            if ohlc_source:
                                results[record['name']] = ohlc_source.get(price_key, 0.0)
                return results

            if status_text:
                status_text.text(f"Fetching Futures Data... (0/{total_records})")
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_to_chunk = {executor.submit(fetch_futures_chunk, chunk): i for i, chunk in enumerate(chunks)}
                
                completed_count = 0
                for future in concurrent.futures.as_completed(future_to_chunk):
                    try:
                        chunk_results = future.result()
                        future_prices.update(chunk_results)
                    except Exception as e:
                        print(f"Error fetching futures chunk: {e}")
                        pass
                    
                    completed_count += 1
                    progress = min((completed_count / total_chunks) * 0.5, 0.5)
                    if progress_bar:
                        progress_bar.progress(progress)
                    if status_text:
                        status_text.text(f"Fetching Futures Data... ({completed_count * chunk_size}/{total_records})")

            # Prepare Option Keys to Fetch
            option_keys_to_fetch = []
            symbol_atm_map = {} # {symbol: {atm_strike, ce_key, pe_key}}
            
            relevant_options_df = options_df[
                (options_df['instrument_type'].isin(['CE', 'PE'])) &
                (options_df['name'].isin(future_prices.keys()))
            ]
            
            if not relevant_options_df.empty:
                 relevant_options_df['expiry_date_obj'] = relevant_options_df['expiry_date'].dt.date
            
            options_grouped = relevant_options_df.groupby('name')
            
            for i, (symbol, ref_price) in enumerate(future_prices.items()):
                if ref_price <= 0: continue
                
                if symbol not in options_grouped.groups:
                    continue
                    
                opts_group = options_grouped.get_group(symbol)
                
                f_rec = unique_futures[unique_futures['name'] == symbol].iloc[0]
                f_expiry = f_rec['expiry_date'].date()
                
                # Extract short symbol for display
                short_symbol = f_rec.get('asset_symbol') or f_rec.get('underlying_symbol') or symbol
                
                opts = opts_group[opts_group['expiry_date_obj'] == f_expiry]
                
                if opts.empty: continue
                
                unique_strikes = sorted(opts['strike_price'].unique())
                if not unique_strikes: continue
                
                atm_strike = min(unique_strikes, key=lambda x: abs(x - ref_price))
                
                ce_row = opts[(opts['strike_price'] == atm_strike) & (opts['instrument_type'] == 'CE')]
                pe_row = opts[(opts['strike_price'] == atm_strike) & (opts['instrument_type'] == 'PE')]
                
                ce_key = ce_row.iloc[0]['instrument_key'] if not ce_row.empty else None
                pe_key = pe_row.iloc[0]['instrument_key'] if not pe_row.empty else None
                
                ce_lot = ce_row.iloc[0]['lot_size'] if not ce_row.empty else 0
                pe_lot = pe_row.iloc[0]['lot_size'] if not pe_row.empty else 0
                
                symbol_atm_map[symbol] = {
                    'ref_price': ref_price,
                    'atm_strike': atm_strike,
                    'ce_key': ce_key,
                    'pe_key': pe_key,
                    'ce_lot': ce_lot,
                    'pe_lot': pe_lot,
                    'display_symbol': short_symbol
                }
                
                if ce_key: option_keys_to_fetch.append(ce_key)
                if pe_key: option_keys_to_fetch.append(pe_key)
                
            # Batch Fetch Options Data
            options_data_map = {}
            total_opt_keys = len(option_keys_to_fetch)
            
            def fetch_options_chunk(chunk_keys):
                keys_str = ",".join(chunk_keys)
                return get_ltp(keys_str, access_token)

            if total_opt_keys > 0:
                opt_chunks = [option_keys_to_fetch[i:i+chunk_size] for i in range(0, total_opt_keys, chunk_size)]
                total_opt_chunks = len(opt_chunks)
                
                if status_text:
                    status_text.text(f"Fetching Options Data... (0/{total_opt_keys})")
                
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    future_to_chunk = {executor.submit(fetch_options_chunk, chunk): i for i, chunk in enumerate(opt_chunks)}
                    
                    completed_count = 0
                    for future in concurrent.futures.as_completed(future_to_chunk):
                        try:
                            ltp_data = future.result()
                            if ltp_data:
                                options_data_map.update(ltp_data)
                        except Exception as e:
                            print(f"Error fetching options chunk: {e}")
                            pass
                        
                        completed_count += 1
                        progress = 0.5 + min((completed_count / total_opt_chunks) * 0.5, 0.5)
                        if progress_bar:
                            progress_bar.progress(progress)
                        if status_text:
                            status_text.text(f"Fetching Options Data... ({completed_count * chunk_size}/{total_opt_keys})")

            # Cleanup Progress
            if progress_bar:
                progress_bar.progress(1.0)
            if status_text:
                status_text.text("Finalizing Data...")
            time.sleep(0.5)
            if progress_bar:
                progress_bar.empty()
            if status_text:
                status_text.empty()
    
    # Common Logic to Process Results (Outside the if/else)
    # This part was common in both branches, so we can keep it here.
    # However, since I duplicated the fetching logic (which is slightly different for progress bars),
    # I need to ensure variables like options_data_map, symbol_atm_map, total_opt_keys are available.
    
    if total_opt_keys > 0 and not options_data_map:
            if not client_view:
                st.error("Failed to fetch Options Data (LTP). Please check your Access Token or Internet Connection.")

    # Optimize Lookup for Options
    # Create a fast lookup map for options_data_map
    fast_options_map = {}
    for k, v in options_data_map.items():
        fast_options_map[k] = v
        
        # Map by Token from the VALUE object if available
        # This is the most robust way because the API might return keys in a different format
        if isinstance(v, dict) and 'instrument_token' in v:
            token = v['instrument_token']
            if token:
                fast_options_map[str(token)] = v
        
        # Fallback: Map by Token (suffix) from the key
        # Key format is typically SEGMENT|TOKEN or SEGMENT:TOKEN
        try:
            token_from_key = k.replace(':', '|').split('|')[-1]
            fast_options_map[token_from_key] = v
        except:
            pass
        
    # DEBUG: Show sample of map if empty
    if not fast_options_map and total_opt_keys > 0:
            st.warning(f"Options Data Map is empty! Sent {total_opt_keys} keys.")

    # Construct Final DataFrame
    final_rows = []
    for symbol, info in symbol_atm_map.items():
        row = {
            "Stock Name": info.get('display_symbol', symbol),
            future_col_name: info['ref_price'],
            "ATM Strike": info['atm_strike']
        }
        
        # Helper to get data with fallback
        def get_opt_data(key):
            if not key: return None
            # Try exact match
            d = fast_options_map.get(key)
            if d: return d
            
            # Try token match (extract token from request key)
            try:
                tok = key.replace(':', '|').split('|')[-1]
                return fast_options_map.get(tok)
            except:
                return None

        # CE Data
        ce_key = info['ce_key']
        ce_ltp = 0
        ce_pct = 0
        ce_vol = 0
        ce_ctr = 0
        
        if ce_key:
            # Optimized lookup
            data = get_opt_data(ce_key)
            if data:
                ce_ltp = data.get('last_price', 0)
                ce_vol = data.get('volume', 0)
                ce_pct = calc_pct_change(ce_ltp, data.get('cp', 0))
                # Calculate Contracts
                lot_size = info.get('ce_lot', 0)
                if lot_size > 0 and ce_vol > 0:
                        ce_ctr = ce_vol / lot_size
        
        row["CE LTP"] = ce_ltp
        row["CE Change %"] = round(ce_pct, 2)
        row["CE Volume"] = ce_vol
        row["CE Contracts"] = int(ce_ctr)
        
        # PE Data
        pe_key = info['pe_key']
        pe_ltp = 0
        pe_pct = 0
        pe_vol = 0
        pe_ctr = 0
        
        if pe_key:
            # Optimized lookup
            data = get_opt_data(pe_key)
            if data:
                pe_ltp = data.get('last_price', 0)
                pe_vol = data.get('volume', 0)
                pe_pct = calc_pct_change(pe_ltp, data.get('cp', 0))
                # Calculate Contracts
                lot_size = info.get('pe_lot', 0)
                if lot_size > 0 and pe_vol > 0:
                        pe_ctr = pe_vol / lot_size
                
        row["PE LTP"] = pe_ltp
        row["PE Change %"] = round(pe_pct, 2)
        row["PE Volume"] = pe_vol
        row["PE Contracts"] = int(pe_ctr)
        
        final_rows.append(row)
        
    df_results = pd.DataFrame(final_rows)

    # Ensure ATM Strike is numeric and rounded for clean display
    if not df_results.empty and "ATM Strike" in df_results.columns:
        df_results["ATM Strike"] = df_results["ATM Strike"].astype(float).round(2)
    
    # Fixed Stock Name to prevent table shaking
    stock_col_name = "Stock Name"
    
    # Save snapshot to session state
    st.session_state['data_snapshot'] = {
        'df': df_results,
        'stock_col_name': stock_col_name,
        'future_col_name': future_col_name
    }

# --- Display Logic (from Session State) ---
if 'data_snapshot' in st.session_state and st.session_state['data_snapshot']:
    snapshot = st.session_state['data_snapshot']
    df_results = snapshot['df']
    stock_col_name = snapshot['stock_col_name']
    
    # Use the stored future_col_name if available, otherwise fallback to current global
    snap_future_col = snapshot.get('future_col_name', future_col_name)
    
    # Check if the column actually exists (in case of stale state mismatch)
    if snap_future_col not in df_results.columns:
        # Try to find a column starting with "Future"
        fut_cols = [c for c in df_results.columns if str(c).startswith("Future")]
        if fut_cols:
            snap_future_col = fut_cols[0]
    
    # Split into CE and PE DataFrames
    ce_cols = [c for c in [stock_col_name, snap_future_col, "ATM Strike", "CE LTP", "CE Change %", "CE Volume", "CE Contracts"] if c in df_results.columns]
    pe_cols = [c for c in [stock_col_name, snap_future_col, "ATM Strike", "PE LTP", "PE Change %", "PE Volume", "PE Contracts"] if c in df_results.columns]
    
    df_ce = df_results[ce_cols].copy()
    df_pe = df_results[pe_cols].copy()
    
    # Auto-Sort by Change % (Descending)
    if not df_ce.empty and "CE Change %" in df_ce.columns:
        df_ce = df_ce.sort_values(by="CE Change %", ascending=False)
    if not df_pe.empty and "PE Change %" in df_pe.columns:
        df_pe = df_pe.sort_values(by="PE Change %", ascending=False)

    # Rename columns for compact display
    
    # Create combined Symbol column
    if not df_ce.empty:
        # Format strike to remove decimals if integer
        df_ce['TempStrike'] = df_ce['ATM Strike'].apply(lambda x: f"{int(x)}" if x == int(x) else f"{x}")
        # Assuming stock_col_name is 'Stock Name' in df_ce before rename
        # But wait, rename happens AFTER this block in my previous code?
        # No, I am editing the block where rename happens.
        # df_ce currently has columns from ce_cols which includes stock_col_name ("Stock Name") and "ATM Strike"
        
        # Use underlying symbol if available, otherwise stock name
        # Since we don't have underlying symbol column in df_results explicitly separate from Stock Name (which is Name),
        # We will just use the Stock Name. If it's long, we might need to truncate, but user asked to combine.
        # "JUBLFOOD 525 CE"
        
        df_ce['DisplaySymbol'] = df_ce[stock_col_name].astype(str) + " " + df_ce['TempStrike'] + " CE"
        
    if not df_pe.empty:
        df_pe['TempStrike'] = df_pe['ATM Strike'].apply(lambda x: f"{int(x)}" if x == int(x) else f"{x}")
        df_pe['DisplaySymbol'] = df_pe[stock_col_name].astype(str) + " " + df_pe['TempStrike'] + " PE"

    rename_map_ce = {
        "DisplaySymbol": "Symbol",
        snap_future_col: "Open",
        "CE LTP": "LTP",
        "CE Change %": "Chg%",
        "CE Volume": "Vol",
        "CE Contracts": "Ctr"
    }
    rename_map_pe = {
        "DisplaySymbol": "Symbol",
        snap_future_col: "Open",
        "PE LTP": "LTP",
        "PE Change %": "Chg%",
        "PE Volume": "Vol",
        "PE Contracts": "Ctr"
    }
    
    # Select only the columns we want to show
    # We drop Stock Name and ATM Strike
    
    if not df_ce.empty:
        df_ce = df_ce.rename(columns=rename_map_ce)
        # Reorder to put Symbol first
        cols = ["Symbol", "Open", "LTP", "Chg%", "Vol", "Ctr"]
        # Filter strictly
        df_ce = df_ce[[c for c in cols if c in df_ce.columns]]
        
    if not df_pe.empty:
        df_pe = df_pe.rename(columns=rename_map_pe)
        cols = ["Symbol", "Open", "LTP", "Chg%", "Vol", "Ctr"]
        df_pe = df_pe[[c for c in cols if c in df_pe.columns]]

    # Display Side-by-Side
    
    # Display Last Updated Time (outside table to prevent shaking)
    time_str = get_ist_now().strftime("%H:%M:%S")
    st.markdown(f"<h5 style='text-align: center; color: #333; margin-bottom: 5px;'>Last Updated: {time_str}</h5>", unsafe_allow_html=True)
    
    col1, col2 = st.columns(2)
    
    table_height = (max(len(df_ce), len(df_pe)) + 1) * 35 + 3

    with col1:
        if not client_view:
             st.subheader("CE Data (Sorted by Change %)")
        
        # Apply Styling
        styler_ce = df_ce.style.set_properties(**{'background-color': '#e6ffe6', 'color': 'black'})
        # Apply white background to Symbol
        if "Symbol" in df_ce.columns:
            styler_ce = styler_ce.set_properties(subset=["Symbol"], **{'background-color': 'white'})
        
        st.dataframe(
            styler_ce,
            column_config={
                "Symbol": st.column_config.TextColumn(label="Symbol"),
                "Open": st.column_config.NumberColumn(format="%.2f"),
                "LTP": st.column_config.NumberColumn(format="%.2f"),
                "Chg%": st.column_config.NumberColumn(format="%.2f%%"),
                "Ctr": st.column_config.NumberColumn(format="%d"),
            },
            use_container_width=True,
            hide_index=True,
            height=table_height
        )

    with col2:
        if not client_view:
             st.subheader("PE Data (Sorted by Change %)")
             
        # Apply Styling
        styler_pe = df_pe.style.set_properties(**{'background-color': '#ffe6e6', 'color': 'black'})
        # Apply white background to Symbol
        if "Symbol" in df_pe.columns:
            styler_pe = styler_pe.set_properties(subset=["Symbol"], **{'background-color': 'white'})
        
        st.dataframe(
            styler_pe,
            column_config={
                "Symbol": st.column_config.TextColumn(label="Symbol"),
                "Open": st.column_config.NumberColumn(format="%.2f"),
                "LTP": st.column_config.NumberColumn(format="%.2f"),
                "Chg%": st.column_config.NumberColumn(format="%.2f%%"),
                "Ctr": st.column_config.NumberColumn(format="%d"),
            },
            use_container_width=True,
            hide_index=True,
            height=table_height
        )

# Handle Auto-Refresh Loop
if should_run and auto_refresh:
    # is_market_closed is defined inside the if should_run block above
    if not is_market_closed:
        time.sleep(refresh_interval)
        st.rerun()

if not should_run and 'data_snapshot' not in st.session_state:
    if not client_view:
        st.info("Click 'Load All Stocks Data' or enable 'Auto-Refresh' in the sidebar to start.")
