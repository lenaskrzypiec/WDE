import pandas as pd
import requests
from datetime import date, timedelta
from google.cloud import bigquery
from google.cloud.exceptions import NotFound

client = bigquery.Client()
logs = []


def get_data(table_name: str):
    try:
        query_str = f'SELECT * FROM `{table_name}`'
        results = client.query(query_str).result()
        df = results.to_dataframe()
        return df
    except NotFound:
        print(f"Brak tabeli {table_name} w BigQuery.")
        return pd.DataFrame()


def dates_check(df: pd.DataFrame):
    yesterday = date.today() - timedelta(days=1)
    df = df[df['data'] <= yesterday].reset_index(drop=True)

    if df.empty:
        print('Brak danych historycznych do przetworzenia')
        return df

    return df


def add_geocoding(df: pd.DataFrame):
    cities_unique = df['miasto'].unique()
    records = list()

    for city in cities_unique:
        url = f'https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=pl&format=json'
        response = requests.get(url)

        if response.status_code == 200:
            data = response.json()

            if 'results' in data and len(data['results']) > 0:
                lat = data['results'][0]['latitude']
                lon = data['results'][0]['longitude']

                records.append({'miasto': city, 'latitude': lat, 'longitude': lon})
            else:
                print(f"Geocoding API: Nie znaleziono miasta '{city}' (literówka lub brak w bazie API).")
                logs.append({'timestamp': pd.Timestamp.now(), 'API': 'Geocoding API', 'kategoria': 'Błędne miasto',
                             'miasto': city, 'komunikat': 'Literówka lub brak miasta w API'})
        else:
            print(f"Geocoding API Error: Serwer zwrócił status {response.status_code} dla miasta '{city}'.")
            logs.append({'timestamp': pd.Timestamp.now(), 'API': 'Geocoding API', 'kategoria': 'Błędna odpowiedź API',
                         'miasto': city,
                         'komunikat': f'Status code: {response.status_code}'})

    df_geo = pd.DataFrame(records)

    if df_geo.empty or 'miasto' not in df_geo.columns:
        df_final = df.copy()
        df_final['latitude'] = None
        df_final['longitude'] = None
        return df_final

    df_final = pd.merge(df, df_geo, on='miasto', how='left')
    return df_final


def add_meteo_data(df: pd.DataFrame):
    df_complete = df.dropna(subset=['latitude', 'longitude']).copy()

    if df_complete.empty:
        return pd.DataFrame(), pd.DataFrame()

    df_complete['data'] = pd.to_datetime(df_complete['data'], errors='coerce')
    df_complete = df_complete.dropna(subset=['data'])

    grouped = df_complete.groupby(['latitude', 'longitude'])['data'].agg(['min', 'max']).to_dict('index')

    coordinates_unique = df_complete[['latitude', 'longitude']].drop_duplicates().values

    daily_records = list()
    hourly_records = list()

    for lat, lon in coordinates_unique:
        city = df_complete[(df_complete['latitude'] == lat) & (df_complete['longitude'] == lon)]['miasto'].iloc[0]

        start_date = grouped[(lat, lon)]['min'].strftime('%Y-%m-%d')
        end_date = grouped[(lat, lon)]['max']

        url = f'https://historical-forecast-api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&start_date={start_date}&end_date={end_date}&daily=temperature_2m_max,temperature_2m_min,temperature_2m_mean,rain_sum,sunrise,sunset&hourly=temperature_2m&timezone=auto'
        response = requests.get(url)

        if response.status_code == 200:
            data = response.json()

            if 'daily' not in data or len(data['daily'].get('time', [])) == 0:
                print(
                    f"Historical Forecast API: Brak danych pogodowych dla miasta '{city}' w zakresie {start_date} - {end_date}.")
                logs.append({'timestamp': pd.Timestamp.now(), 'API': 'Historical Forecast API',
                             'kategoria': 'Brak danych dla dnia/miasta', 'miasto': city,
                             'komunikat': f'Brak danych pogodowych w API dla zakresu {start_date} - {end_date}'})
                continue

            daily = data['daily']
            for i in range(len(daily['time'])):
                daily_records.append({
                    'latitude': lat,
                    'longitude': lon,
                    'data': pd.to_datetime(daily['time'][i]),
                    'sr_temp': daily['temperature_2m_mean'][i],
                    'max_temp': daily['temperature_2m_max'][i],
                    'min_temp': daily['temperature_2m_min'][i],
                    'suma_opadow': daily['rain_sum'][i],
                    'wschod': daily['sunrise'][i],
                    'zachod': daily['sunset'][i]
                })

            hourly = data['hourly']
            for i in range(len(hourly['time'])):
                classic_date = hourly['time'][i].split('T')[0]

                hourly_records.append({
                    'latitude': lat,
                    'longitude': lon,
                    'data': pd.to_datetime(classic_date),
                    'data_godzina': hourly['time'][i],
                    'temperatura': hourly['temperature_2m'][i]
                })
        else:
            print(f"Historical Forecast API Error: Serwer zwrócił status {response.status_code} dla miasta '{city}'.")
            logs.append(
                {'timestamp': pd.Timestamp.now(), 'API': 'Historical Forecast API', 'kategoria': 'Błędna odpowiedź API',
                 'miasto': city,
                 'komunikat': f'Status code: {response.status_code}'})

    if not daily_records or not hourly_records:
        return pd.DataFrame(), pd.DataFrame()

    df_daily_data = pd.DataFrame(daily_records)
    df_hourly_data = pd.DataFrame(hourly_records)

    df_daily_final = pd.merge(df_complete, df_daily_data, on=['latitude', 'longitude', 'data'], how='inner')
    df_hourly_final = pd.merge(df_complete, df_hourly_data, on=['latitude', 'longitude', 'data'], how='inner')

    df_daily_final['data'] = df_daily_final['data']
    df_daily_final = df_daily_final[
        ['miasto', 'data', 'sr_temp', 'max_temp', 'min_temp', 'suma_opadow', 'wschod', 'zachod']]
    df_daily_final['data'] = pd.to_datetime(df_daily_final['data']).dt.date
    df_daily_final['wschod'] = pd.to_datetime(df_daily_final['wschod'], format='ISO8601').dt.time
    df_daily_final['zachod'] = pd.to_datetime(df_daily_final['zachod'], format='ISO8601').dt.time

    df_hourly_final = df_hourly_final[['miasto', 'data_godzina', 'temperatura']]
    df_hourly_final['data_godzina'] = pd.to_datetime(df_hourly_final['data_godzina'])

    return df_daily_final, df_hourly_final


def filter_new_data(df_new: pd.DataFrame, df_old: pd.DataFrame, key_cols: list):
    if df_old.empty:
        return df_new

    df_old_keys = df_old[key_cols].copy()
    df_new_keys = df_new[key_cols].copy()

    df_combined = pd.concat([df_old_keys, df_new_keys], ignore_index=True)
    is_duplicate = df_combined.duplicated(subset=key_cols)
    is_new_record = ~is_duplicate.iloc[len(df_old):]

    df_new = df_new[is_new_record.values].reset_index(drop=True)

    return df_new


def push_data(df: pd.DataFrame, table_name: str):
    if df.empty:
        print(f'Brak nowych danych dla tabeli: {table_name}')
        return

    job_config = bigquery.LoadJobConfig()
    job_config.write_disposition = 'WRITE_APPEND'
    job = client.load_table_from_dataframe(dataframe=df, destination=table_name, job_config=job_config)
    job.result()

    print(f'Dane pomyślnie wgrane do {table_name}')


def main():
    tabela_wejsciowa = 'wne-dev-1234.piwo_ls463932.Miasta_daty'
    tabela_dzienna = 'wne-dev-1234.piwo_ls463932.pogoda_dane_dzienne'
    tabela_godzinowa = 'wne-dev-1234.piwo_ls463932.pogoda_dane_godzinowe'

    df = get_data(table_name=tabela_wejsciowa)

    if df.empty:
        print('Tabela wejściowa jest pusta lub brak tabeli wejściowej w BigQuery')
        return

    df = dates_check(df)

    if df.empty:
        return

    df_old_daily = get_data(tabela_dzienna)
    df_new_records = filter_new_data(df_new=df, df_old=df_old_daily, key_cols=['miasto', 'data'])
    if df_new_records.empty:
        print('Brak nowych danych historycznych do przetworzenia')
        return

    df_geo = add_geocoding(df_new_records)
    df_daily, df_hourly = add_meteo_data(df_geo)

    if not df_daily.empty:
        push_data(df=df_daily, table_name=tabela_dzienna)
    else:
        print("Brak danych dziennych z API do przetworzenia.")

    if not df_hourly.empty:
        push_data(df=df_hourly, table_name=tabela_godzinowa)
    else:
        print("Brak danych godzinowych z API do przetworzenia.")

    if logs:
        tabela_logow = 'wne-dev-1234.piwo_ls463932.pogoda_logi_bledow'
        push_data(df=pd.DataFrame(logs), table_name=tabela_logow)
        print(f"Pomyślnie zapisano logi błędów do tabeli ({len(logs)}).")


if __name__ == '__main__':
    main()
