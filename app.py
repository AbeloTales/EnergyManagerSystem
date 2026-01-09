import sqlite3
import cv2
import pytesseract
import RPi.GPIO as GPIO
import time
from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta

app = Flask(__name__)

# --- CONFIGURACIÓN ---
DB_NAME = 'energia.db'
CONSUMO_LIMITE_ALERTA = 5.0  # Si el consumo (Delta) sube de esto, se apagan los NO prioritarios
config_tesseract = r'--oem 3 --psm 6 outputbase digits'

# --- GESTIÓN DE HARDWARE ---
def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Verificamos si la tabla existe por seguridad antes de leer
    try:
        c.execute("SELECT pin_gpio, estado FROM reles")
        for row in c.fetchall():
            pin, estado = row
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, not estado) # Lógica inversa: 1 (ON) -> GPIO LOW
    except:
        pass # Si falla es porque no se ha inicializado la DB aun
    conn.close()

# --- CEREBRO INTELIGENTE: HORARIOS Y PRIORIDAD ---
def verificar_inteligencia():
    """Revisa cada minuto si hay que encender/apagar por horario"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    hora_actual = datetime.now().strftime("%H:%M")

    try:
        c.execute("SELECT * FROM grupos WHERE usar_horario = 1")
        grupos = c.fetchall()
        
        for g in grupos:
            inicio = g['hora_inicio']
            fin = g['hora_fin']
            gid = g['id']
            
            # Lógica de intervalo (incluso si cruza medianoche)
            encender = False
            if inicio < fin:
                if inicio <= hora_actual <= fin: encender = True
            else: # Cruza medianoche (ej: 22:00 a 06:00)
                if hora_actual >= inicio or hora_actual <= fin: encender = True
            
            # Aplicar estado al grupo (Solo actualizamos la DB y pines)
            nuevo_estado = 1 if encender else 0
            
            # Actualizamos relés de este grupo
            c.execute("UPDATE reles SET estado=? WHERE id_grupo=?", (nuevo_estado, gid))
            
            # Actualizamos físico
            c.execute("SELECT pin_gpio FROM reles WHERE id_grupo=?", (gid,))
            for r in c.fetchall():
                GPIO.output(r['pin_gpio'], not nuevo_estado)
        
        conn.commit()
    except Exception as e:
        print(f"Error inteligencia: {e}")
    finally:
        conn.close()

def gestion_carga_critica(consumo_actual):
    """Apaga grupos de BAJA prioridad si el consumo es excesivo"""
    if consumo_actual > CONSUMO_LIMITE_ALERTA:
        print(f"⚠️ ALERTA: Consumo alto ({consumo_actual}). Apagando cargas bajas.")
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        # Obtener grupos de baja prioridad (0)
        c.execute("SELECT id FROM grupos WHERE prioridad = 0")
        grupos_baja = c.fetchall()
        
        for g in grupos_baja:
            gid = g[0]
            c.execute("UPDATE reles SET estado=0 WHERE id_grupo=?", (gid,))
            c.execute("SELECT pin_gpio FROM reles WHERE id_grupo=?", (gid,))
            for r in c.fetchall():
                GPIO.output(r[0], True) # Apagar (HIGH)
        
        conn.commit()
        conn.close()

# --- TAREA DE MONITOREO (CÁMARA) ---
def tarea_monitoreo_energia():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened(): return
    cap.set(3, 640); cap.set(4, 480)
    ret, frame = cap.read()
    cap.release()

    if ret:
        try:
            # Procesamiento de imagen
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
            texto = pytesseract.image_to_string(thresh, config=config_tesseract)
            texto_limpio = ''.join(filter(str.isdigit, texto))
            
            if len(texto_limpio) > 0:
                lectura_actual = float(texto_limpio)
                
                # Calcular consumo real (Diferencia con anterior)
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("SELECT valor_kwh FROM lecturas ORDER BY id DESC LIMIT 1")
                ultima = c.fetchone()
                
                delta = 0.0
                if ultima:
                    anterior = ultima[0]
                    if lectura_actual >= anterior:
                        delta = lectura_actual - anterior
                
                # Guardar
                c.execute("INSERT INTO lecturas (valor_kwh, consumo_delta) VALUES (?, ?)", 
                          (lectura_actual, delta))
                conn.commit()
                conn.close()
                
                # Verificar si debemos apagar cosas
                gestion_carga_critica(delta)
                
        except Exception as e:
            print(f"Error lectura: {e}")

# --- RUTAS DE LA API ---

@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/api/datos')
def api_datos():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 1. Gráfico (Últimos 20 deltas)
    c.execute("SELECT fecha, consumo_delta FROM lecturas ORDER BY id DESC LIMIT 20")
    grafico = [list(row) for row in c.fetchall()][::-1]
    
    # 2. Estadísticas y Predicción
    # Tomamos los últimos 10 para promediar
    vals = [x[1] for x in grafico[-10:]] if grafico else [0]
    promedio = sum(vals) / len(vals) if vals else 0
    # Predicción simple (Promedio * minutos en un mes)
    prediccion = promedio * 43200 # Ejemplo: 30 días * 24h * 60m
    
    tendencia = "Estable"
    if vals and vals[-1] > promedio * 1.1: tendencia = "Subiendo"
    elif vals and vals[-1] < promedio * 0.9: tendencia = "Bajando"

    # 3. Dispositivos y Grupos
    c.execute("SELECT * FROM reles")
    reles = [dict(row) for row in c.fetchall()]
    
    c.execute("SELECT * FROM grupos")
    grupos = [dict(row) for row in c.fetchall()]
    
    # 4. Historial Tabla
    c.execute("SELECT * FROM lecturas ORDER BY id DESC LIMIT 50")
    historial = [dict(row) for row in c.fetchall()]
    
    conn.close()
    
    return jsonify({
        'grafico': grafico,
        'reles': reles,
        'grupos': grupos,
        'historial': historial,
        'estadisticas': {
            'promedio': round(promedio, 2),
            'tendencia': tendencia,
            'prediccion': round(prediccion, 2)
        }
    })

@app.route('/api/control', methods=['POST'])
def api_control():
    data = request.json
    accion = data.get('accion')
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    try:
        # TOGGLE INDIVIDUAL
        if accion == 'toggle':
            rid = data.get('id')
            c.execute("SELECT estado, pin_gpio FROM reles WHERE id=?", (rid,))
            r = c.fetchone()
            if r:
                nuevo = 1 - r[0]
                GPIO.output(r[1], not nuevo)
                c.execute("UPDATE reles SET estado=? WHERE id=?", (nuevo, rid))
        
        # CAMBIAR NOMBRE (Recuperado)
        elif accion == 'editar_nombre':
            rid = data.get('id')
            nom = data.get('nombre')
            c.execute("UPDATE reles SET nombre=? WHERE id=?", (nom, rid))

        # CONTROL DE GRUPO (ON/OFF Completo)
        elif accion == 'grupo':
            gid = data.get('id_grupo')
            est = int(data.get('estado'))
            c.execute("UPDATE reles SET estado=? WHERE id_grupo=?", (est, gid))
            c.execute("SELECT pin_gpio FROM reles WHERE id_grupo=?", (gid,))
            for r in c.fetchall():
                GPIO.output(r[0], not est)

        # CONTROL GLOBAL
        elif accion == 'global':
            est = int(data.get('estado'))
            c.execute("UPDATE reles SET estado=?", (est,))
            c.execute("SELECT pin_gpio FROM reles")
            for r in c.fetchall():
                GPIO.output(r[0], not est)

        # CREAR GRUPO INTELIGENTE
        elif accion == 'crear_grupo':
            c.execute("INSERT INTO grupos (nombre, prioridad, usar_horario, hora_inicio, hora_fin) VALUES (?,?,?,?,?)",
                      (data.get('nombre'), int(data.get('prioridad')), int(data.get('usar_horario')), 
                       data.get('hora_inicio'), data.get('hora_fin')))
            gid = c.lastrowid
            for rid in data.get('reles'):
                c.execute("UPDATE reles SET id_grupo=? WHERE id=?", (gid, rid))

        # ELIMINAR GRUPO
        elif accion == 'eliminar_grupo':
            gid = data.get('id_grupo')
            c.execute("UPDATE reles SET id_grupo=0 WHERE id_grupo=?", (gid,))
            c.execute("DELETE FROM grupos WHERE id=?", (gid,))

        conn.commit()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500
    finally:
        conn.close()

if __name__ == '__main__':
    setup_gpio()
    # Programador de tareas (Background)
    sched = BackgroundScheduler()
    sched.add_job(tarea_monitoreo_energia, 'interval', minutes=1)
    sched.add_job(verificar_inteligencia, 'interval', minutes=1)
    sched.start()
    
    app.run(host='0.0.0.0', port=80, debug=False, use_reloader=False)