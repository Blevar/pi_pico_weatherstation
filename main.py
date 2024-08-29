from machine import I2C, Pin, SPI
import machine
import time
import network
import socket
import bme280_float as bme280
from ssd1306 import SSD1306_I2C
import _thread
import ntptime
import ujson
import json
import sdcard
import os
import gc
import datetime

# Ustawienie początkowe
time.sleep(5)

# Initialize the SD card
spi = SPI(1, baudrate=40000000, sck=Pin(10), mosi=Pin(11), miso=Pin(12))
sd = sdcard.SDCard(spi, Pin(13))

# Create an instance of MicroPython Unix-like Virtual File System (VFS)
vfs = os.VfsFat(sd)
 
# Mount the SD card
os.mount(sd, '/sd')

# Debug print SD card directory and files
print(os.listdir('/sd'))

# Initialize the onboard LED as output
led = Pin("LED", machine.Pin.OUT)

# Konfiguracja I2C
i2c = I2C(0, scl=Pin(1), sda=Pin(0), freq=400000)

# Inicjalizacja sensorów
bme280 = bme280.BME280(i2c=i2c)
oled = SSD1306_I2C(128, 32, i2c)

# Konfiguracja anemometru
wind_pin = Pin(28, Pin.IN, Pin.PULL_UP)
wind_pulses = 0
last_wind_time = time.time()
wind_speed = 0
min_wind_speed = float('inf')
max_wind_speed = float('-inf')

# Globalne zmienne
temperature = 0
humidity = 0
pressure = 0
min_temperature = float('inf')
max_temperature = float('-inf')
min_humidity = float('inf')
max_humidity = float('-inf')
min_pressure = float('inf')
max_pressure = float('-inf')
wifi_connected = False
last_update_time = time.time()
last_web_update_time = time.time()

# Funkcja do tworzenia odpowiednich katalogów na karcie SD
def create_directory_structure():
    now = time.localtime()  # Pobierz lokalny czas jako tuple (year, month, day, ...)
    year_dir = f"/sd/{now[0]}"
    month_dir = f"{year_dir}/{now[1]:02d}"
    day_dir = f"{month_dir}/{now[2]:02d}"

    for dir_path in [year_dir, month_dir, day_dir]:
        try:
            os.mkdir(dir_path)
        except OSError as e:
            if e.args[0] != 17:  # Ignoruj błąd "File exists"
                raise

    return day_dir

# Funkcja do zapisywania danych na karcie SD
def save_data_to_sd():
    global temperature, humidity, pressure, min_temperature, max_temperature, min_humidity, max_humidity, min_pressure, max_pressure
    global wind_speed, min_wind_speed, max_wind_speed

    day_dir = create_directory_structure()
    now = time.localtime()  # Pobierz lokalny czas jako tuple (year, month, day, hour, ...)
    filename = f"{day_dir}/{now[0]}-{now[1]:02d}-{now[2]:02d}-{now[3]:02d}.txt"

    # Obliczanie średnich wartości dla bieżącej godziny
    avg_temp = temperature
    avg_humidity = humidity
    avg_pressure = pressure
    avg_wind_speed = wind_speed

    data = "Avg Temp: {}C, Min Temp: {}C, Max Temp: {}C\n" \
           "Avg Hum: {}%, Min Hum: {}%, Max Hum: {}%\n" \
           "Avg Press: {}hPa, Min Press: {}hPa, Max Press: {}hPa\n" \
           "Avg Wind Speed: {} m/s, Min Wind Speed: {} m/s, Max Wind Speed: {} m/s\n".format(
               avg_temp, min_temperature, max_temperature, 
               avg_humidity, min_humidity, max_humidity, 
               avg_pressure, min_pressure, max_pressure,
               avg_wind_speed, min_wind_speed, max_wind_speed
           )

    with open(filename, "w") as file:
        file.write(data)

    print(f"Data saved to {filename}")

    # Po zapisaniu danych z godziny, resetuj wartości min/max
    min_temperature = float('inf')
    max_temperature = float('-inf')
    min_humidity = float('inf')
    max_humidity = float('-inf')
    min_pressure = float('inf')
    max_pressure = float('-inf')
    min_wind_speed = float('inf')
    max_wind_speed = float('-inf')

# Funkcja do czyszczenia starych danych na karcie SD
def clean_old_data():
    now = datetime.time.time()
    current_year = now.year
    current_month = now.month

    for year in os.listdir("/sd"):
        year_path = f"/sd/{year}"
        if int(year) < current_year - 1:
            # Usuń katalogi starsze niż rok
            for root, dirs, files in os.walk(year_path, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.rmdir(year_path)
            print(f"Deleted directory {year_path}")
        else:
            for month in os.listdir(year_path):
                month_path = f"{year_path}/{month}"
                if int(year) == current_year and int(month) >= current_month:
                    continue

                for day in os.listdir(month_path):
                    day_path = f"{month_path}/{day}"
                    day_dir_files = sorted(os.listdir(day_path))

                    if int(year) == current_year and int(month) == current_month - 1:
                        # Usuń pliki, zostawiając tylko dane co godzinę dla poprzedniego miesiąca
                        if len(day_dir_files) > 1:
                            for file in day_dir_files[:-1]:
                                os.remove(f"{day_path}/{file}")
                    else:
                        # Usuwanie danych starszych niż miesiąc, pozostawiając jedynie dzienne podsumowania
                        if len(day_dir_files) > 1:
                            os.remove(f"{day_path}/{day_dir_files[0]}")
                        for file in day_dir_files[1:]:
                            os.remove(f"{day_path}/{file}")

                # Usuń puste katalogi dnia
                if not os.listdir(day_path):
                    os.rmdir(day_path)
                print(f"Processed {month_path}")

    # Usuwanie pustych katalogów miesiąca
    for year in os.listdir("/sd"):
        year_path = f"/sd/{year}"
        for month in os.listdir(year_path):
            month_path = f"{year_path}/{month}"
            if not os.listdir(month_path):
                os.rmdir(month_path)

    # Usuwanie pustych katalogów roku
    for year in os.listdir("/sd"):
        year_path = f"/sd/{year}"
        if not os.listdir(year_path):
            os.rmdir(year_path)

# Funkcja do odczytu danych z BME280
def read_sensor_data():
    global temperature, humidity, pressure
    temperature, pressure, humidity = bme280.values
    
    temperature = float(temperature[:-1])
    pressure = float(pressure[:-3])
    humidity = float(humidity[:-1])
    
    # Aktualizacja wartości min/max
    global min_temperature, max_temperature, min_humidity, max_humidity, min_pressure, max_pressure
    if temperature < min_temperature:
        min_temperature = temperature
    if temperature > max_temperature:
        max_temperature = temperature
    if humidity < min_humidity:
        min_humidity = humidity
    if humidity > max_humidity:
        max_humidity = humidity
    if pressure < min_pressure:
        min_pressure = pressure
    if pressure > max_pressure:
        max_pressure = pressure

# Funkcja do obliczania prędkości wiatru
def calculate_wind_speed():
    global wind_pulses, wind_speed, last_wind_time
    current_time = time.time()
    elapsed_time = current_time - last_wind_time
    if elapsed_time != 0:
        wind_pulses_per_second = wind_pulses / elapsed_time
        wind_speed = wind_pulses_per_second * 0.0875  # Prędkość wiatru w m/s
        last_wind_time = current_time
        wind_pulses = 0

    # Aktualizacja wartości min/max
    global min_wind_speed, max_wind_speed
    if wind_speed < min_wind_speed:
        min_wind_speed = wind_speed
    if wind_speed > max_wind_speed:
        max_wind_speed = wind_speed

# Funkcja do aktualizacji wyświetlacza OLED
def update_display():
    oled.fill(0)
    oled.text(str(temperature) + "C, " + str(humidity) + "%", 0, 0)
    oled.text("Pres: " + str(pressure) + "hPa", 0, 11)
    oled.text("Wind: " + str(wind_speed) + " m/s", 0, 22)
    oled.show()

def get_ntp_time():
    try:
        ntptime.settime()
        print("NTP time synchronized")
    except Exception as e:
        print(f"Failed to synchronize time: {e}")

def connect(ssid, password):
    global wifi_connected
    
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(ssid, password)
    print("Connecting to WiFi...")
    
    # Oczekiwanie na połączenie
    i = 0    
    while wlan.isconnected() == False:
        led.value(i % 2)
        i = (i + 1) % 2
        time.sleep(1)
        
    print("wlan.isconnected(): " + str(wlan.isconnected()))    
    
    if wlan.isconnected():
        ip = wlan.ifconfig()[0]        
        print(f'WiFi Connected on {ip}')        
        wifi_connected = True
        led.value(1)
        return ip
    else:
        print("Failed to connect to WiFi.")
        wifi_connected = False
        led.value(0)
        return None

# Wątek 1: Odczyt danych z BME280, obliczanie prędkości wiatru i aktualizacja wyświetlacza
def sensor_thread():
    global temperature, humidity, pressure
    global last_update_time
    global wind_pulses, wind_speed, last_wind_time

    while True:
        try:
            read_sensor_data()
            calculate_wind_speed()
            update_display()
            current_time = time.time()

            # Reset wartości min/max co 24 godziny
            if (current_time - last_update_time) > 86400:  # 86400 sekund = 24 godziny
                global min_temperature, max_temperature, min_humidity, max_humidity, min_pressure, max_pressure
                min_temperature = float('inf')
                max_temperature = float('-inf')
                min_humidity = float('inf')
                max_humidity = float('-inf')
                min_pressure = float('inf')
                max_pressure = float('-inf')
                min_wind_speed = float('inf')
                max_wind_speed = float('-inf')
                last_update_time = current_time

            # Zapisywanie danych co godzinę
            if (current_time - last_update_time) >= 3600:
                save_data_to_sd()
                last_update_time = current_time

            # Czyszczenie starych danych raz dziennie
            if (current_time - last_update_time) >= 86400:
                clean_old_data()

            if gc.mem_free() < 5000:
                gc.collect()

            time.sleep(1)

        except Exception as e:
            print(f"Unhandled exception in sensor_thread: {e}")
            time.sleep(5)  # Opóźnienie przed ponownym uruchomieniem wątku

# Funkcja do ładowania konfiguracji Wi-Fi z pliku
def load_wifi_config(filename):
    config = {}
    try:
        with open(filename, 'r') as file:
            for line in file:
                key, value = line.strip().split('=')
                config[key] = value
    except OSError as e:
        print("Failed to load Wi-Fi config: {}".format(e))
    return config       

def read_txt_data(file_path):
    try:
        with open(file_path, 'r') as file:
            lines = file.readlines()
        
        labels = []
        temperatures = []
        humidities = []
        pressures = []
        wind_speeds = []
        
        for line in lines:
            parts = line.strip().split(',')
            if len(parts) != 5:
                continue
            
            timestamp = int(parts[0])
            labels.append(time.strftime('%Y-%m-%d %H:%M', time.localtime(timestamp)))
            temperatures.append(float(parts[1]))
            humidities.append(float(parts[2]))
            pressures.append(float(parts[3]))
            wind_speeds.append(float(parts[4]))

        return {
            'labels': labels,
            'temperatures': temperatures,
            'humidities': humidities,
            'pressures': pressures,
            'wind_speeds': wind_speeds
        }
    except OSError as e:
        print(f"Failed to read file {file_path}: {e}")
        return None

def generate_html():
    try:
        with open('index2.html', 'r') as file:
            html = file.read()
            
        html = html.replace('{{temperature}}', str(temperature))
        html = html.replace('{{humidity}}', str(humidity))
        html = html.replace('{{pressure}}', str(pressure))
        html = html.replace('{{wind_speed}}', str(wind_speed))
        html = html.replace('{{min_temperature}}', str(min_temperature))
        html = html.replace('{{max_temperature}}', str(max_temperature))
        html = html.replace('{{min_humidity}}', str(min_humidity))
        html = html.replace('{{max_humidity}}', str(max_humidity))
        html = html.replace('{{min_pressure}}', str(min_pressure))
        html = html.replace('{{max_pressure}}', str(max_pressure))
        html = html.replace('{{min_wind_speed}}', str(min_wind_speed))
        html = html.replace('{{max_wind_speed}}', str(max_wind_speed))

        # Ścieżki do plików tekstowych na karcie SD
        data_24h = read_txt_data('/sd/data_24h.txt')
        data_7d = read_txt_data('/sd/data_7d.txt')
        data_30d = read_txt_data('/sd/data_30d.txt')
        data_90d = read_txt_data('/sd/data_90d.txt')

        # Zastąpienie placeholderów rzeczywistymi danymi
        if data_24h:
            html = html.replace('{{chart24h_labels}}', json.dumps(data_24h['labels']))
            html = html.replace('{{chart24h_temperatures}}', json.dumps(data_24h['temperatures']))
            html = html.replace('{{chart24h_humidities}}', json.dumps(data_24h['humidities']))
            html = html.replace('{{chart24h_pressures}}', json.dumps(data_24h['pressures']))
            html = html.replace('{{chart24h_wind_speeds}}', json.dumps(data_24h['wind_speeds']))
        
        if data_7d:
            html = html.replace('{{chart7d_labels}}', json.dumps(data_7d['labels']))
            html = html.replace('{{chart7d_temperatures}}', json.dumps(data_7d['temperatures']))
            html = html.replace('{{chart7d_humidities}}', json.dumps(data_7d['humidities']))
            html = html.replace('{{chart7d_pressures}}', json.dumps(data_7d['pressures']))
            html = html.replace('{{chart7d_wind_speeds}}', json.dumps(data_7d['wind_speeds']))
        
        if data_30d:
            html = html.replace('{{chart30d_labels}}', json.dumps(data_30d['labels']))
            html = html.replace('{{chart30d_temperatures}}', json.dumps(data_30d['temperatures']))
            html = html.replace('{{chart30d_humidities}}', json.dumps(data_30d['humidities']))
            html = html.replace('{{chart30d_pressures}}', json.dumps(data_30d['pressures']))
            html = html.replace('{{chart30d_wind_speeds}}', json.dumps(data_30d['wind_speeds']))

        if data_90d:
            html = html.replace('{{chart90d_labels}}', json.dumps(data_90d['labels']))
            html = html.replace('{{chart90d_temperatures}}', json.dumps(data_90d['temperatures']))
            html = html.replace('{{chart90d_humidities}}', json.dumps(data_90d['humidities']))
            html = html.replace('{{chart90d_pressures}}', json.dumps(data_90d['pressures']))
            html = html.replace('{{chart90d_wind_speeds}}', json.dumps(data_90d['wind_speeds']))

        return "HTTP/1.1 200 OK\nContent-Type: text/html; charset=utf-8\n\n" + html
    except OSError as e:
        print(f"Failed to load index.html: {e}")
        return "HTTP/1.1 500 Internal Server Error\nContent-Type: text/plain\n\nFailed to load page"

# Funkcja obsługująca impulsy z anemometru
def wind_interrupt(pin):
    global wind_pulses
    wind_pulses += 1

# Wątek do obsługi Wi-Fi i serwera WWW
def wifi_thread():
    global wifi_connected
    global last_web_update_time

    config = load_wifi_config('wifi_config.txt')
    ssid = config.get('SSID', '')
    password = config.get('PASSWORD', '')

    # Ustawienie przerwania na pinie anemometru
    wind_pin.irq(trigger=Pin.IRQ_RISING, handler=wind_interrupt)

    while True:
        if not wifi_connected:
            ip = connect(ssid, password)
            if ip:
                print(f"Connected to WiFi at {ip}")
            time.sleep(5)

        try:
            get_ntp_time()
        except Exception as e:
            print(f"Failed to synchronize time: {e}")

        try:
            addr = socket.getaddrinfo('0.0.0.0', 80)[0][-1]
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(addr)
            s.listen(1)
            print(f"Server listening on {addr}")

            while wifi_connected:
                try:
                    cl, addr = s.accept()
                    print(f'Client connected from {addr}')
                    request = cl.recv(1024)
                    #print(f'Request: {request}')

                    current_time = time.time()
                    if (current_time - last_web_update_time) > 3600:
                        last_web_update_time = current_time
                        cl.close()
                        break

                    response = generate_html()
                    cl.send(response)
                    cl.close()

                except Exception as e:
                    print(f"Error handling client: {e}")
                finally:
                    cl.close()

        except Exception as e:
            print(f"Error in web server: {e}")

        finally:
            s.close()
            wifi_connected = False
            print("Socket closed, retrying...")
            time.sleep(5)

# Uruchomienie wątków
_thread.start_new_thread(sensor_thread, ())
wifi_thread()