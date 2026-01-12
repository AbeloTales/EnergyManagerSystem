import sqlite3
import cv2
import pytesseract
import RPi.GPIO as GPIO
import time
import requests
import base64
import numpy as np # Necesitamos numpy para operaciones de engrosamiento
from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

app = Flask(__name__)

# --- CONFIGURACIÓN ---
DB_NAME = 'energia.db'
COSTO_KWH = 0.10 
# Configuración Tesseract: --psm 7 trata la imagen como una sola línea de texto
config_tesseract = r'--oem 3 --psm 7 outputbase digits'

# --- GESTIÓN DE HARDWARE ---
def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        c.execute("SELECT pin_gpio, estado FROM reles")
        for row in c.fetchall():
            pin, estado = row
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, not estado) 
    except: pass
    conn.close()

# --- FUNCIÓN CENTRAL DE PROCESAMIENTO DE IMAGEN ---
def procesar_imagen_ocr(frame):
    """
    Convierte una imagen de números blancos/LCD en algo legible para Tesseract.
    Retorna: (imagen_procesada, texto_leido)
    """
    # 1. Escala de grises
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    
    # 2. Aumentar el contraste (Hacer lo blanco más blanco y lo negro más negro)
    # alpha = contraste (1.0-3.0), beta = brillo (0-100)
    contraste = cv2.convertScaleAbs(gray, alpha=1.5, beta=10)

    # 3. Umbralización (THRESHOLD)
    # THRESH_BINARY_INV: Lo que es BLANCO (números) se vuelve NEGRO.
    # Lo que es NEGRO (fondo) se vuelve BLANCO.
    # Tesseract ama el texto negro sobre fondo blanco.
    thresh = cv2.threshold(contraste, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]

    # 4. Operación Morfológica (Engrosar texto)
    # Los números digitales a veces son muy finos o están segmentados.
    # Esto los "conecta".
    kernel = np.ones((3,3), np.uint8)
    # ERODE en imagen invertida = Engrosar las letras negras
    procesada = cv2.erode(thresh, kernel, iterations=1) 
    
    # 5. Lectura OCR
    txt = pytesseract.image_to_string(procesada, config=config_tesseract)
    clean = ''.join(filter(str.isdigit, txt))
    
    return procesada, clean

# --- TELEGRAM ---
def enviar_telegram(mensaje, forzar=False):
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM config WHERE id=1")
        cfg = c.fetchone()
        token = cfg['telegram_token']
        chat_id = cfg['telegram_chat_id']
        last_alert = cfg['ultima_alerta']
        conn.close()

        if not token or not chat_id: return False

        now = datetime.now()
        if not forzar and last_alert:
            last_time = datetime.strptime(last_alert, "%Y-%m-%d %H:%M:%S")
            if (now - last_time).total_seconds() < 1800: return False

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": mensaje}
        r = requests.post(url, json=payload)
        
        if not forzar and r.status_code == 200:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("UPDATE config SET ultima_alerta=? WHERE id=1", (now.strftime("%Y-%m-%d %H:%M:%S"),))
            conn.commit()
            conn.close()
        return r.status_code == 200
    except Exception as e: 
        print(f"Error Telegram: {e}")
        return False

# --- CEREBRO: CONTROL Y ALERTAS ---
def verificar_sistema():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now = datetime.now()
    hora_actual = now.strftime("%H:%M")
    
    c.execute("SELECT * FROM config WHERE id=1")
    cfg = c.fetchone()
    set_point = cfg['set_point_dinero']
    
    c.execute("SELECT consumo_delta FROM lecturas ORDER BY id DESC LIMIT 10")
    vals = [r[0] for r in c.fetchall()]
    promedio = sum(vals)/len(vals) if vals else 0
    prediccion_costo = (promedio * 43200) * COSTO_KWH
    
    limitado_por_costo = False
    if prediccion_costo > set_point:
        limitado_por_costo = True
        enviar_telegram(f"⚠️ PRECAUCIÓN: Consumo proyectado (${prediccion_costo:.2f}) supera presupuesto. Apagando grupos no esenciales.")
        c.execute("SELECT id, pin_gpio FROM reles WHERE id_grupo IN (SELECT id FROM grupos WHERE prioridad=0)")
        for r in c.fetchall():
            c.execute("UPDATE reles SET estado=0 WHERE id=?", (r['id'],))
            GPIO.output(r['pin_gpio'], True)

    if promedio > 0 and vals and vals[0] < (promedio * 0.1): 
        enviar_telegram("⚠️ ALERTA: Consumo inusualmente bajo detectado.")

    c.execute("SELECT * FROM grupos")
    grupos = c.fetchall()
    for g in grupos:
        if limitado_por_costo and g['prioridad'] == 0: continue
        gid = g['id']
        nuevo_estado = None
        
        if g['usar_horario'] == 1:
            ini, fin = g['hora_inicio'], g['hora_fin']
            encender = (ini <= hora_actual < fin) if ini < fin else (hora_actual >= ini or hora_actual < fin)
            nuevo_estado = 1 if encender else 0

        if g['modo_ciclo'] == 1:
            last = g['ultima_accion']
            min_on, min_off = g['ciclo_on'], g['ciclo_off']
            c.execute("SELECT estado FROM reles WHERE id_grupo=? LIMIT 1", (gid,))
            estado_actual = c.fetchone()[0] if c.fetchone() else 0

            if not last:
                nuevo_estado = 1
                c.execute("UPDATE grupos SET ultima_accion=? WHERE id=?", (now.strftime("%Y-%m-%d %H:%M:%S"), gid))
            else:
                last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                diff = (now - last_dt).total_seconds() / 60
                if estado_actual == 1 and diff >= min_on: 
                    nuevo_estado = 0
                    c.execute("UPDATE grupos SET ultima_accion=? WHERE id=?", (now.strftime("%Y-%m-%d %H:%M:%S"), gid))
                elif estado_actual == 0 and diff >= min_off:
                    nuevo_estado = 1
                    c.execute("UPDATE grupos SET ultima_accion=? WHERE id=?", (now.strftime("%Y-%m-%d %H:%M:%S"), gid))

        if nuevo_estado is not None:
            c.execute("UPDATE reles SET estado=? WHERE id_grupo=?", (nuevo_estado, gid))
            c.execute("SELECT pin_gpio FROM reles WHERE id_grupo=?", (gid,))
            for r in c.fetchall(): GPIO.output(r[0], not nuevo_estado)

    conn.commit()
    conn.close()

# --- TAREA OCR (AUTOMÁTICA) ---
def tarea_monitoreo():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened(): return
    cap.set(3, 640); cap.set(4, 480)
    ret, frame = cap.read()
    cap.release()
    
    if ret:
        try:
            # Usamos la nueva función de procesamiento centralizado
            _, clean = procesar_imagen_ocr(frame)
            
            if len(clean) > 0:
                val = float(clean)
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("SELECT valor_kwh FROM lecturas ORDER BY id DESC LIMIT 1")
                last = c.fetchone()
                
                delta = 0.0
                if last and val >= last[0]:
                    delta = val - last[0]
                
                # Filtro: Ignorar lecturas absurdas (ej: 0.0 o saltos gigantes por error OCR)
                if val > 0 and delta < 100: 
                    c.execute("INSERT INTO lecturas (valor_kwh, consumo_delta) VALUES (?, ?)", (val, delta))
                    conn.commit()
                conn.close()
        except: pass

# --- RUTAS ---
@app.route('/')
def usuario(): return render_template('usuario.html')

@app.route('/tecnico')
def tecnico(): return render_template('tecnico.html')

@app.route('/api/datos')
def api_datos():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT fecha, consumo_delta FROM lecturas ORDER BY id DESC LIMIT 30")
    grafico = [list(r) for r in c.fetchall()][::-1] 
    vals = [x[1] for x in grafico[-10:]] if grafico else [0]
    prom = sum(vals)/len(vals) if vals else 0
    pred_kwh = prom * 43200
    pred_usd = pred_kwh * COSTO_KWH
    c.execute("SELECT * FROM config WHERE id=1")
    cfg = c.fetchone()
    c.execute("SELECT * FROM reles")
    reles = [dict(r) for r in c.fetchall()]
    c.execute("SELECT * FROM grupos")
    grupos = [dict(r) for r in c.fetchall()]
    conn.close()
    hora_servidor = datetime.now().strftime("%H:%M:%S")
    return jsonify({
        'grafico': grafico, 'reles': reles, 'grupos': grupos,
        'config': dict(cfg),
        'stats': {'prom': round(prom, 2), 'pred_kwh': round(pred_kwh, 2), 'pred_usd': round(pred_usd, 2)},
        'hora_servidor': hora_servidor 
    })

@app.route('/api/test_telegram', methods=['POST'])
def test_telegram():
    exito = enviar_telegram("🔔 PRUEBA DE SISTEMA:\n¡Conexión exitosa con el Monitor de Energía!", forzar=True)
    if exito: return jsonify({'status': 'ok'})
    else: return jsonify({'status': 'error', 'msg': 'Fallo al enviar. Verifique Token/ChatID'})

# --- DEBUG CÁMARA (VISUALIZACIÓN) ---
@app.route('/api/debug_camara')
def debug_camara():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened(): return jsonify({'status': 'error', 'msg': 'No hay cámara'})
    cap.set(3, 640); cap.set(4, 480)
    ret, frame = cap.read()
    cap.release()
    
    if not ret: return jsonify({'status': 'error', 'msg': 'Error captura'})

    # Usamos la misma función que usa el robot para ver exactamente lo mismo
    imagen_procesada, lectura = procesar_imagen_ocr(frame)
    
    # Convertir a JPG para enviar al navegador
    _, buffer = cv2.imencode('.jpg', imagen_procesada)
    img_str = base64.b64encode(buffer).decode('utf-8')
    
    return jsonify({'status': 'ok', 'imagen': img_str, 'lectura_detectada': lectura if lectura else "Nada"})

@app.route('/api/guardar_config', methods=['POST'])
def guardar_config():
    d = request.json
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    if 'set_point' in d:
        c.execute("UPDATE config SET set_point_dinero=? WHERE id=1", (d['set_point'],))
    if 'telegram_token' in d:
        c.execute("UPDATE config SET telegram_token=?, telegram_chat_id=? WHERE id=1", 
                  (d['telegram_token'], d['telegram_chat_id']))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/control', methods=['POST'])
def api_control():
    data = request.json
    accion = data.get('accion')
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        if accion == 'toggle':
            rid = data.get('id')
            c.execute("SELECT estado, pin_gpio FROM reles WHERE id=?", (rid,))
            r = c.fetchone()
            if r:
                nuevo = 1 - r[0]
                GPIO.output(r[1], not nuevo)
                c.execute("UPDATE reles SET estado=? WHERE id=?", (nuevo, rid))
        elif accion == 'editar_nombre':
            c.execute("UPDATE reles SET nombre=? WHERE id=?", (data.get('nombre'), data.get('id')))
        elif accion == 'grupo':
            gid = int(data.get('id_grupo'))
            est = int(data.get('estado'))
            c.execute("UPDATE reles SET estado=? WHERE id_grupo=?", (est, gid))
            c.execute("SELECT pin_gpio FROM reles WHERE id_grupo=?", (gid,))
            for r in c.fetchall(): GPIO.output(r[0], not est)
        elif accion == 'global':
            est = int(data.get('estado'))
            c.execute("UPDATE reles SET estado=?", (est,))
            c.execute("SELECT pin_gpio FROM reles")
            for r in c.fetchall(): GPIO.output(r[0], not est)
        elif accion == 'crear_grupo':
            c.execute("INSERT INTO grupos (nombre, prioridad, usar_horario, hora_inicio, hora_fin, modo_ciclo, ciclo_on, ciclo_off) VALUES (?,?,?,?,?,?,?,?)",
                      (data.get('nombre'), int(data.get('prioridad')), int(data.get('usar_horario')), 
                       data.get('hora_inicio'), data.get('hora_fin'), 
                       int(data.get('modo_ciclo',0)), int(data.get('ciclo_on',0)), int(data.get('ciclo_off',0))))
            gid = c.lastrowid
            for rid in data.get('reles'): c.execute("UPDATE reles SET id_grupo=? WHERE id=?", (gid, rid))
        elif accion == 'editar_grupo':
            gid = data.get('id')
            c.execute("""UPDATE grupos SET nombre=?, prioridad=?, usar_horario=?, hora_inicio=?, hora_fin=?, 
                      modo_ciclo=?, ciclo_on=?, ciclo_off=?, ultima_accion=NULL WHERE id=?""",
                      (data.get('nombre'), int(data.get('prioridad')), int(data.get('usar_horario')), 
                       data.get('hora_inicio'), data.get('hora_fin'), 
                       int(data.get('modo_ciclo',0)), int(data.get('ciclo_on',0)), int(data.get('ciclo_off',0)), gid))
            c.execute("UPDATE reles SET id_grupo=0 WHERE id_grupo=?", (gid,))
            for rid in data.get('reles'): c.execute("UPDATE reles SET id_grupo=? WHERE id=?", (gid, rid))
        elif accion == 'eliminar_grupo':
            gid = data.get('id_grupo')
            c.execute("UPDATE reles SET id_grupo=0 WHERE id_grupo=?", (gid,))
            c.execute("DELETE FROM grupos WHERE id=?", (gid,))
        conn.commit()
        return jsonify({'status': 'ok'})
    except Exception as e: return jsonify({'status': 'error', 'msg': str(e)}), 500
    finally: conn.close()

if __name__ == '__main__':
    setup_gpio()
    sched = BackgroundScheduler()
    sched.add_job(tarea_monitoreo, 'interval', seconds=15)
    sched.add_job(verificar_sistema, 'interval', minutes=1)
    sched.start()
    app.run(host='0.0.0.0', port=80, debug=False, use_reloader=False)