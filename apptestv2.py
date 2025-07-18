from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from kafka import KafkaProducer
import json
from threading import Lock
from datetime import datetime, date


last_assigned_counter_idx = 0
last_assigned_pharm_idx = 0

app = Flask(__name__)
CORS(app)

app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://neondb_owner:npg_kuFcm04EIoUT@ep-gentle-voice-a199bnjz-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

PATIENT_PRIORITY = {
    'Emergency': 1,
    'Vip': 2,
    'Regular': 3
}

# --- Models ---
class Doctor(db.Model):
    __tablename__ = 'doctors'
    docid = db.Column(db.String, primary_key=True)
    docname = db.Column(db.String)
    roomno = db.Column(db.String)
    docspec = db.Column(db.String)
    isactive=db.Column(db.Boolean,default=True, nullable=False)

class Patient(db.Model):
    __tablename__ = 'patients'
    uhid = db.Column(db.String, primary_key=True)
    patientname = db.Column(db.String)
    age = db.Column(db.Integer)
    bloodgroup = db.Column(db.String)
    patienttype = db.Column(db.String)
    phone_number = db.Column(db.String)

class TokenMap(db.Model):
    __tablename__ = 'tokenmap'
    tokenno = db.Column(db.Integer, primary_key=True)
    uhid = db.Column(db.String)
    time = db.Column(db.DateTime)
    docid = db.Column(db.String)


class VitalsLog(db.Model):
    __tablename__ = 'vitallogs'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    counterno = db.Column(db.Integer)
    uhid = db.Column(db.String)
    tokenno = db.Column(db.Integer)
    date = db.Column(db.Date)
    starttime = db.Column(db.DateTime, nullable=True)
    endtime = db.Column(db.DateTime, nullable=True)

class CounterInfo(db.Model):
    __tablename__ = 'counterinfo'
    counterno = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String)
    counterloc = db.Column(db.String)
    queueinfo = db.Column(db.Text)  # stores JSON list


class ConsultLog(db.Model):
    __tablename__ = 'consultlogs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    roomno = db.Column(db.String, nullable=False)
    tokenno = db.Column(db.Integer, nullable=False)
    uhid = db.Column(db.String, nullable=False)
    docid = db.Column(db.String, nullable=False)
    starttime = db.Column(db.Time, nullable=True)
    endtime = db.Column(db.Time, nullable=True)
    date = db.Column(db.Date, nullable=False)

    def __repr__(self):
        return (f"<ConsultLog roomno={self.roomno} "
                f"tokenno={self.tokenno} uhid={self.uhid} docid={self.docid} "
                f"starttime={self.starttime} endtime={self.endtime} date={self.date}>")
    


class DocRoomInfo(db.Model):
    __tablename__ = 'docroominfo'

    roomno = db.Column(db.String, primary_key=True)
    queueinfo = db.Column(db.Text, nullable=True, default='[]') # JSON-encoded list of token numbers
    docname = db.Column(db.String, nullable=False)
    docid = db.Column(db.String, unique=True, nullable=False)
    isactive = db.Column(db.Boolean, default=True, nullable=False)

    def __repr__(self):
        return (f"<DocRoomInfo roomno={self.roomno} docid={self.docid} "
                f"docname={self.docname} isactive={self.isactive} queueinfo={self.queueinfo}>")
    

class PharmacyInfo(db.Model):
    __tablename__ = 'pharmacyinfo'
    pharmid = db.Column(db.Integer, primary_key=True)
    pharmname = db.Column(db.String)
    status = db.Column(db.Boolean, default=True, nullable=False)
    queueinfo = db.Column(db.Text, nullable=True, default='[]')
    counterloc = db.Column(db.String, nullable=True)

class PharmLog(db.Model):
    __tablename__ = 'pharmlog'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    pharmid = db.Column(db.Integer, nullable=False)  # FK, but not enforced (optional)
    tokenno = db.Column(db.Integer, nullable=False)
    uhid = db.Column(db.String, nullable=False)
    date = db.Column(db.Date, nullable=False)
    starttime = db.Column(db.DateTime, nullable=True)
    endtime = db.Column(db.DateTime, nullable=True)


# --- Token Generation Logic ---
token_counter = 1
last_reset_date = datetime.now().date()
token_lock = Lock()

KAFKA_BOOTSTRAP_SERVERS = 'localhost:9092'
KAFKA_TOPIC = 'vitals_queue'
producer = KafkaProducer(
    bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

# --- Helper for Priority ---
def get_patient_priority(uhid):
    patient = Patient.query.filter_by(uhid=uhid).first()
    return PATIENT_PRIORITY.get(patient.patienttype, 3) if patient else 3




# --- API Endpoints  -   Token Generation  ---




@app.route('/', methods=['GET'])
def test():
     return jsonify({
        'test': 'working',
    })

@app.route('/api/generate_token', methods=['GET'])
def generate_token():
    global token_counter, last_reset_date, last_assigned_counter_idx

    uhid = request.args.get('uhid')
    docid = request.args.get('docid')
    if not uhid or not docid:
        return jsonify({'error': 'Missing UHID or docid'}), 400

    patient = Patient.query.filter_by(uhid=uhid).first()
    if not patient:
        return jsonify({'error': 'Patient not found'}), 404

    PATIENT_PRIORITY = {'Emergency': 1, 'Vip': 2, 'Regular': 3}
    patient_priority = PATIENT_PRIORITY.get(patient.patienttype, 3)

    today = datetime.now().date()
    with token_lock:
        if today != last_reset_date:
            token_counter = 1
            last_reset_date = today
        token_number = token_counter
        token_counter += 1
        current_time = datetime.now()

    doctor = Doctor.query.filter_by(docid=str(docid)).first()
    if not doctor:
        return jsonify({'error': 'Doctor not found'}), 404

    # Fetch all open counters (status == True for boolean)
    counters = CounterInfo.query.filter(CounterInfo.status == True).order_by(CounterInfo.counterno).all()
    if not counters:
        return jsonify({'error': 'No open counters available'}), 400

    # Calculate queue lengths for all counters
    queue_lengths = []
    for c in counters:
        if c.queueinfo:
            if isinstance(c.queueinfo, str):
                try:
                    queue = json.loads(c.queueinfo)
                except Exception:
                    queue = []
            elif isinstance(c.queueinfo, list):
                queue = c.queueinfo
            else:
                queue = []
        else:
            queue = []
        queue_lengths.append(len(queue))

    # Find all counters with the shortest queue length
    min_queue_len = min(queue_lengths)
    eligible_counters = [c for c, l in zip(counters, queue_lengths) if l == min_queue_len]
    eligible_counters.sort(key=lambda c: c.counterno)

    # Use round-robin among eligible counters
    selected_counter = eligible_counters[last_assigned_counter_idx % len(eligible_counters)]
    last_assigned_counter_idx += 1

    counterno = selected_counter.counterno

    # Store in tokenmap
    new_tokenmap = TokenMap(
        tokenno=token_number,
        uhid=uhid,
        time=current_time,
        docid=str(docid)
    )
    db.session.add(new_tokenmap)
    db.session.commit()

    # Add entry to vitallogs
    vitallog_entry = VitalsLog(
        counterno=counterno,
        uhid=uhid,
        tokenno=token_number,
        date=today,
        starttime=None,
        endtime=None
    )
    db.session.add(vitallog_entry)
    db.session.commit()

    # Insert token into queue by priority
    if selected_counter.queueinfo:
        if isinstance(selected_counter.queueinfo, str):
            try:
                queue = json.loads(selected_counter.queueinfo)
            except Exception:
                queue = []
        elif isinstance(selected_counter.queueinfo, list):
            queue = selected_counter.queueinfo
        else:
            queue = []
    else:
        queue = []

    def get_patient_priority_for_token(tokenno):
        v_log = VitalsLog.query.filter_by(tokenno=tokenno, counterno=counterno).first()
        if v_log:
            p = Patient.query.filter_by(uhid=v_log.uhid).first()
            return PATIENT_PRIORITY.get(p.patienttype, 3) if p else 3
        return 3

    insert_idx = len(queue)
    for idx, token in enumerate(queue):
        p_priority = get_patient_priority_for_token(token)
        if patient_priority < p_priority:
            insert_idx = idx
            break
    queue.insert(insert_idx, token_number)
    selected_counter.queueinfo = json.dumps(queue)
    db.session.commit()

    return jsonify({
        'uhid': uhid,
        'token_number': token_number,
        'datetime': current_time.isoformat(),
        'docid': docid,
        'counterno': counterno
    })




# --- API Endpoints  -   Vitals  ---





@app.route('/api/vitals/queue', methods=['GET'])
def get_vitals_queue():
    today = datetime.now().date()
    logs = VitalsLog.query.filter_by(date=today).filter(VitalsLog.endtime==None).all()
    PATIENT_PRIORITY = {'Emergency': 1, 'Vip': 2, 'Regular': 3}

    queue = []
    for log in logs:
        patient = Patient.query.filter_by(uhid=log.uhid).first()
        priority = PATIENT_PRIORITY.get(patient.patienttype, 3) if patient else 3
        queue.append({
            'counterno': log.counterno,
            'uhid': log.uhid,
            'tokenno': log.tokenno,
            'priority': priority,  # temporary, for sorting
            'starttime': log.starttime.isoformat() if log.starttime else None,
            'endtime': log.endtime.isoformat() if log.endtime else None,


        })

    # Sort by priority, then by tokenno (FIFO within each priority)
    queue.sort(key=lambda x: (x['counterno'], x['priority'], x['tokenno']))

    # Remove 'priority' key before returning
    for item in queue:
        item.pop('priority')

    return jsonify(queue)


@app.route('/api/vitals/reported', methods=['POST'])
def reported_for_vitals():
    data = request.json
    tokenno = data.get('tokenno')
    counterno = data.get('counterno')
    today = datetime.now().date()

    # Check if someone is already in progress at this counter
    in_progress = VitalsLog.query.filter_by(
        counterno=counterno,
        date=today
    ).filter(
        VitalsLog.starttime.isnot(None),
        VitalsLog.endtime.is_(None)
    ).first()
    if in_progress:
        return jsonify({
            "error": "Another patient is currently being processed at this counter. Please wait until they finish."
        }), 409

    log_entry = VitalsLog.query.filter_by(
        tokenno=tokenno,
        counterno=counterno,
        date=today
    ).first()
    if not log_entry:
        return jsonify({'error': 'Vitals log not found'}), 404

    log_entry.starttime = datetime.now()
    db.session.commit()

    return jsonify({
        'status': 'starttime updated',
        'tokenno': tokenno,
        'counterno': counterno,
        'starttime': log_entry.starttime.isoformat()
    })


@app.route('/api/vitals/finished', methods=['POST'])
def finished_vitals():
    data = request.json
    tokenno = data.get('tokenno')
    counterno = data.get('counterno')
    today = datetime.now().date()

    # Update vitals endtime
    log_entry = VitalsLog.query.filter_by(tokenno=tokenno, counterno=counterno, date=today).first()
    if not log_entry:
        return jsonify({'error': 'Vitals log not found'}), 404

    log_entry.endtime = datetime.now()

    # Get token details
    tokenmap_entry = TokenMap.query.filter_by(tokenno=tokenno).first()
    if not tokenmap_entry:
        return jsonify({'error': 'Tokenmap entry not found'}), 404

    uhid = tokenmap_entry.uhid
    docid = tokenmap_entry.docid

    # Get doctor room
    roominfo = DocRoomInfo.query.filter_by(docid=docid).first()
    if not roominfo or not roominfo.isactive:
        return jsonify({'error': 'Doctor room not found or inactive'}), 404

    # Load current queue
    queue = []
    if roominfo.queueinfo:
        try:
            queue = json.loads(roominfo.queueinfo)
        except Exception:
            queue = []

    # Define priority
    PATIENT_PRIORITY = {'Emergency': 1, 'Vip': 2, 'Regular': 3}
    patient = Patient.query.filter_by(uhid=uhid).first()
    current_priority = PATIENT_PRIORITY.get(patient.patienttype, 3)

    # Helper to get queue patient priority
    def get_queue_patient_priority(token):
        tm = TokenMap.query.filter_by(tokenno=token).first()
        if tm and tm.uhid:
            p = Patient.query.filter_by(uhid=tm.uhid).first()
            return PATIENT_PRIORITY.get(p.patienttype, 3) if p else 3
        return 3

    # Insert token into priority position
    insert_idx = len(queue)
    for idx, tok in enumerate(queue):
        if current_priority < get_queue_patient_priority(tok):
            insert_idx = idx
            break

    if tokenno not in queue:
        queue.insert(insert_idx, tokenno)
        roominfo.queueinfo = json.dumps(queue)

    # Add to consultlogs
    new_log = ConsultLog(
        roomno=roominfo.roomno,
        tokenno=tokenno,
        uhid=uhid,
        docid=docid,
        starttime=None,
        endtime=None,
        date=today
    )
    db.session.add(new_log)

    # ✅ Commit everything once at the end
    db.session.commit()

    print(f"[✔] Final doctor queue for room {roominfo.roomno}: {queue}")

    return jsonify({
        'status': 'endtime updated and pushed to doctor queue',
        'tokenno': tokenno,
        'counterno': counterno,
        'endtime': log_entry.endtime.isoformat(),
        'doc_queue_push': {
            'status': 'pushed',
            'roomno': roominfo.roomno,
            'tokenno': tokenno,
            'uhid': uhid
        }
    })




@app.route('/api/doctors')
def get_doctors():
    doctors = Doctor.query.all()
    return jsonify([{
        'DocID': d.docid,
        'DocName': d.docname,
        'RoomNo': d.roomno,
        'DocSpec': d.docspec
    } for d in doctors])

@app.route('/api/patients')
def get_patients():
    uhid = request.args.get('uhid')
    if uhid:
        patient = Patient.query.filter_by(uhid=uhid).first()
        if patient:
            return jsonify({
                'UHID': patient.uhid,
                'PatientName': patient.patientname,
                'Age': patient.age,
                'BloodGroup': patient.bloodgroup,
                'PatientType': patient.patienttype,
                'PhoneNumber': patient.phone_number
            })
        else:
            return jsonify({'error': 'Patient not found'}), 404
    else:
        patients = Patient.query.all()
        return jsonify([{
            'UHID': p.uhid,
            'PatientName': p.patientname,
            'Age': p.age,
            'BloodGroup': p.bloodgroup,
            'PatientType': p.patienttype,
            'PhoneNumber': p.phone_number
        } for p in patients])
    
@app.route('/api/counters/add', methods=['POST'])
def add_counter():
    data = request.get_json()
    # counterloc is optional
    counterloc = data.get('counterloc', None)
    # Find the next available counter number
    max_counter = db.session.query(db.func.max(CounterInfo.counterno)).scalar() or 0
    new_counterno = max_counter + 1
    new_counter = CounterInfo(
        counterno=new_counterno,
        status='TRUE',
        counterloc=counterloc,
        queueinfo=json.dumps([])
    )
    db.session.add(new_counter)
    db.session.commit()
    return jsonify({
        'counterno': new_counterno,
        'status': 'TRUE',
        'counterloc': counterloc,
        'queueinfo': []
    }), 201




@app.route('/api/counters/delete', methods=['POST'])
def delete_counter():
    data = request.get_json()
    counterno = data.get('counterno')
    counter = CounterInfo.query.filter_by(counterno=counterno).first()
    if not counter:
        return jsonify({'error': 'Counter not found'}), 404

    queue = []
    if counter.queueinfo:
        if isinstance(counter.queueinfo, str):
            try:
                queue = json.loads(counter.queueinfo)
            except Exception:
                queue = []
        elif isinstance(counter.queueinfo, list):
            queue = counter.queueinfo

    if queue:
        # Mark for pending delete
        counter.pending_delete = True
        db.session.commit()
        return jsonify({'status': 'pending_delete', 'counterno': counterno})
    else:
        # Safe to delete
        db.session.delete(counter)
        db.session.commit()
        return jsonify({'status': 'deleted', 'counterno': counterno})



@app.route('/api/counters/enable', methods=['POST'])
def enable_counter():
    data = request.get_json()
    counterno = data.get('counterno')
    counter = CounterInfo.query.filter_by(counterno=counterno).first()
    if not counter:
        return jsonify({'error': 'Counter not found'}), 404
    counter.status = True
    db.session.commit()
    return jsonify({'status': 'enabled', 'counterno': counterno})

@app.route('/api/counters/disable', methods=['POST'])
def disable_counter():
    data = request.get_json()
    counterno = data.get('counterno')
    counter = CounterInfo.query.filter_by(counterno=counterno).first()
    if not counter:
        return jsonify({'error': 'Counter not found'}), 404
    
    if counter.queueinfo:
        if isinstance(counter.queueinfo, str):
            try:
                queue = json.loads(counter.queueinfo)
            except Exception:
                queue = []
        elif isinstance(counter.queueinfo, list):
            queue = counter.queueinfo
        else:
            queue = []
    else:
        queue = []

    if queue:
        # If you need a 'disabling' state, use another boolean column
        # For now, keep it enabled until queue is empty
        return jsonify({'status': 'disabling', 'counterno': counterno})
    else:
        counter.status = False
        db.session.commit()
        return jsonify({'status': 'disabled', 'counterno': counterno})

    

@app.route('/api/counters/all', methods=['GET'])
def get_all_counters():
    counters = CounterInfo.query.all()
    result = []
    for counter in counters:
        result.append({
            'counterno': counter.counterno,
            'status': counter.status
        })
    result.sort(key=lambda x: x['counterno'])
    return jsonify(result)





# --- API Endpoints  -  Doctors  ---






@app.route('/api/doctor_queue/reported', methods=['POST'])
def consult_reported():
    data = request.get_json()
    tokenno = data.get('tokenno')
    roomno = data.get('roomno')
    from datetime import datetime
    today = datetime.now().date()

    # Only one patient can be in progress per room
    in_progress = ConsultLog.query.filter_by(
        roomno=roomno, date=today
    ).filter(ConsultLog.starttime.isnot(None), ConsultLog.endtime.is_(None)).first()
    if in_progress:
        return jsonify({"error": "Consultation already in progress for this room."}), 409

    log_entry = ConsultLog.query.filter_by(tokenno=tokenno, roomno=roomno, date=today).first()
    if not log_entry:
        return jsonify({'error': 'Consult log not found'}), 404

    log_entry.starttime = datetime.now().time()
    db.session.commit()
    return jsonify({
        'status': 'consultation started',
        'tokenno': tokenno,
        'roomno': roomno,
        'starttime': log_entry.starttime.isoformat()
    })



@app.route('/api/doctor_queue/finished', methods=['POST'])
def consult_finished():
    data = request.get_json()
    tokenno = data.get('tokenno')
    roomno = data.get('roomno')
    from datetime import datetime
    today = datetime.now().date()

    # --- 1. End the consultation in ConsultLog ---
    log_entry = ConsultLog.query.filter_by(tokenno=tokenno, roomno=roomno, date=today).first()
    if not log_entry:
        return jsonify({'error': 'Consult log not found'}), 404

    log_entry.endtime = datetime.now().time()  # or datetime.now() if you made the column DateTime

    # --- 2. Remove from DocRoom queueinfo ---
    roominfo = DocRoomInfo.query.filter_by(roomno=roomno).first()
    if roominfo:
        queue = []
        if roominfo.queueinfo:
            try:
                queue = json.loads(roominfo.queueinfo)
            except Exception:
                queue = []
        if tokenno in queue:
            queue.remove(tokenno)
            roominfo.queueinfo = json.dumps(queue)

    # --- 3. PUSH TO PHARMACY QUEUE ---
    # Find open pharmacies
    pharm_counters = PharmacyInfo.query.filter_by(status=True).all()
    if not pharm_counters:
        db.session.commit()
        return jsonify({'warning': 'No OPEN pharmacy counters – patient not queued for pharmacy. Token is done with doctor queue.'})

    # Least-loaded/round-robin logic:
    pharm_queues = []
    for c in pharm_counters:
        try:
            pq = json.loads(c.queueinfo) if c.queueinfo else []
        except Exception:
            pq = []
        pharm_queues.append((len(pq), c))

    min_queue_len = min(q[0] for q in pharm_queues)
    eligible = [c for l, c in pharm_queues if l == min_queue_len]
    global last_assigned_pharm_idx
    eligible.sort(key=lambda x: x.pharmid)
    selected_pharm = eligible[last_assigned_pharm_idx % len(eligible)]
    last_assigned_pharm_idx += 1


    # PATIENT PRIORITY LOGIC
    PATIENT_PRIORITY = {'Emergency': 1, 'Vip': 2, 'Regular': 3}
    tokenmap_entry = TokenMap.query.filter_by(tokenno=tokenno).first()
    uhid = tokenmap_entry.uhid if tokenmap_entry else None
    patient = Patient.query.filter_by(uhid=uhid).first()
    current_priority = PATIENT_PRIORITY.get(getattr(patient, "patienttype", None), 3) if patient else 3

    # Insert token in pharmacy queue by priority
    try:
        pharm_queue = json.loads(selected_pharm.queueinfo) if selected_pharm.queueinfo else []
    except Exception:
        pharm_queue = []

    def get_queue_token_priority(tok):
        tm = TokenMap.query.filter_by(tokenno=tok).first()
        if not tm or not tm.uhid:
            return 3
        pat = Patient.query.filter_by(uhid=tm.uhid).first()
        return PATIENT_PRIORITY.get(getattr(pat, "patienttype", None), 3) if pat else 3

    insert_idx = len(pharm_queue)
    for idx, tok in enumerate(pharm_queue):
        if current_priority < get_queue_token_priority(tok):
            insert_idx = idx
            break

    if tokenno not in pharm_queue:
        pharm_queue.insert(insert_idx, tokenno)
        selected_pharm.queueinfo = json.dumps(pharm_queue)

        # Add to PharmLog
        new_log = PharmLog(
            pharmid=selected_pharm.pharmid,
            tokenno=tokenno,
            uhid=uhid,
            date=today,
            starttime=None,
            endtime=None
        )
        db.session.add(new_log)

    db.session.commit()

    return jsonify({
        'status': 'consultation finished and pushed to pharmacy queue',
        'tokenno': tokenno,
        'roomno': roomno,
        'endtime': log_entry.endtime.isoformat() if log_entry.endtime else None,
        'pharm_queue_push': {
            'status': 'pushed',
            'pharmid': selected_pharm.pharmid,
            'tokenno': tokenno,
            'uhid': uhid
        }
    })




@app.route('/api/docrooms/priority_queue', methods=['GET'])
def docrooms_priority_queue():
    today = datetime.now().date()
    all_rooms = DocRoomInfo.query.all()
    PATIENT_PRIORITY = {'Emergency': 1, 'Vip': 2, 'Regular': 3}
    result = []

    for room in all_rooms:
        # All active logs for this room, today, not yet finished
        consults = ConsultLog.query.filter_by(
            roomno=room.roomno, date=today
        ).filter(
            ConsultLog.endtime.is_(None)
        ).all()
        # Decorate each with patient priority for sorting
        enriched = []
        for entry in consults:
            patient = Patient.query.filter_by(uhid=entry.uhid).first()
            priority = PATIENT_PRIORITY.get(getattr(patient, 'patienttype', None), 3) if patient else 3
            enriched.append({
                'tokenno': entry.tokenno,
                'uhid': entry.uhid,
                'docid': entry.docid,
                'priority': priority,
                'logid': entry.id  # use id for FIFO tie
            })
        # Sort: priority first, then FIFO for matching priority
        enriched.sort(key=lambda x: (x['priority'], x['logid']))
        # Remove helper fields before returning
        for x in enriched:
            del x['priority']
            del x['logid']
        result.append({
            'roomno': room.roomno,
            'docname': room.docname,
            'docid': room.docid,
            'isactive': room.isactive,
            'queue': enriched
            
        })
    return jsonify(result)


def set_doctor_and_room_active_status(docid, new_status):
    # Update Doctor
    doctor = Doctor.query.filter_by(docid=str(docid)).first()
    if doctor:
        doctor.isactive = new_status

    # Update Associated Room
    room = DocRoomInfo.query.filter_by(docid=str(docid)).first()
    if room:
        room.isactive = new_status   # assumes attribute in model matches DB column

    db.session.commit()


@app.route('/api/docrooms/disable', methods=['POST'])
def docroom_disable():
    data = request.get_json()
    docid = data.get('docid')
    room = DocRoomInfo.query.filter_by(docid=str(docid)).first()
    doc = Doctor.query.filter_by(docid=str(docid)).first()
    if not room or not doc:
        return jsonify({'error': 'Not found'}), 404
    room.isactive = False
    doc.isactive = False  # assumes column is lowercase
    db.session.commit()
    set_doctor_and_room_active_status(docid, False)
    return jsonify({'status': 'disabled', 'docid': docid})


@app.route('/api/docrooms/enable', methods=['POST'])
def docroom_enable():
    data = request.get_json()
    docid = data.get('docid')
    room = DocRoomInfo.query.filter_by(docid=str(docid)).first()
    doc = Doctor.query.filter_by(docid=str(docid)).first()
    if not room or not doc:
        return jsonify({'error': 'Not found'}), 404
    room.isactive = True
    doc.isactive = True  # assumes column is lowercase
    db.session.commit()
    set_doctor_and_room_active_status(docid, True)
    return jsonify({'status': 'enabled', 'docid': docid})





# --- API Endpoints  -   Pharmacy  ---




@app.route('/api/pharmacy/priority_queue', methods=['GET'])
def pharmacy_priority_queue():
    today = datetime.now().date()
    all_counters = PharmacyInfo.query.all()
    PATIENT_PRIORITY = {'Emergency': 1, 'Vip': 2, 'Regular': 3}
    result = []
    for counter in all_counters:
        # Get all today's active PharmLog tokens for this pharmacy, not finished
        active_logs = PharmLog.query.filter_by(
            pharmid=counter.pharmid, date=today
        ).filter(PharmLog.endtime.is_(None)).all()
        # Build up the enriched queue
        enriched = []
        for entry in active_logs:
            patient = Patient.query.filter_by(uhid=entry.uhid).first()
            priority = PATIENT_PRIORITY.get(getattr(patient, 'patienttype', None), 3) if patient else 3
            enriched.append({
                'tokenno': entry.tokenno,
                'uhid': entry.uhid,
                'priority': priority,
                'starttime': entry.starttime.isoformat() if entry.starttime else None,
                'endtime': entry.endtime.isoformat() if entry.endtime else None,
                'logid': entry.id  # FIFO tie breaking
            })
        # Sort: by priority, then FIFO order
        enriched.sort(key=lambda x: (x['priority'], x['logid']))
        # Remove helper fields before returning
        for x in enriched:
            del x['priority']
            del x['logid']
        result.append({
            'pharmid': counter.pharmid,
            'pharmname': counter.pharmname,
            'status': counter.status,
            'counterloc': counter.counterloc,
            'queue': enriched
        })
    return jsonify(result)




@app.route('/api/pharmacy/reported', methods=['POST'])
def pharmacy_reported():
    data = request.json
    tokenno = data.get('tokenno')
    pharmid = data.get('pharmid')
    today = datetime.now().date()

    log = PharmLog.query.filter_by(tokenno=tokenno, pharmid=pharmid, date=today).first()
    if not log:
        return jsonify({'error': 'Pharm log not found'}), 404
    if log.starttime is not None:
        return jsonify({'error': 'Already started'}), 409

    log.starttime = datetime.now()
    db.session.commit()
    return jsonify({
        'status': 'pharmacy processing started',
        'tokenno': tokenno,
        'pharmid': pharmid,
        'starttime': log.starttime.isoformat()
    })



@app.route('/api/pharmacy/finished', methods=['POST'])
def pharmacy_finished():
    data = request.json
    tokenno = data.get('tokenno')
    pharmid = data.get('pharmid')
    today = datetime.now().date()

    log = PharmLog.query.filter_by(tokenno=tokenno, pharmid=pharmid, date=today).first()
    if not log:
        return jsonify({'error': 'Pharm log not found'}), 404
    log.endtime = datetime.now()

    # Remove from counter queueinfo
    counter = PharmacyInfo.query.filter_by(pharmid=pharmid).first()
    if counter and counter.queueinfo:
        try:
            queue = json.loads(counter.queueinfo)
        except Exception:
            queue = []
        if tokenno in queue:
            queue.remove(tokenno)
            counter.queueinfo = json.dumps(queue)

    db.session.commit()
    return jsonify({
        'status': 'pharmacy processing finished',
        'tokenno': tokenno,
        'pharmid': pharmid,
        'endtime': log.endtime.isoformat()
    })



@app.route('/api/pharmacy/all', methods=['GET'])
def pharmacy_all():
    all_counters = PharmacyInfo.query.all()
    result = []
    for counter in all_counters:
        try:
            queue_tokens = json.loads(counter.queueinfo) if counter.queueinfo else []
        except Exception:
            queue_tokens = []
        result.append({
            'pharmid': counter.pharmid,
            'pharmname': counter.pharmname,
            'status': counter.status,
            'counterloc': counter.counterloc,
            'queue_length': len(queue_tokens)
        })
    result.sort(key=lambda x: x['pharmid'])
    return jsonify(result)




@app.route('/api/pharmacy/enable', methods=['POST'])
def pharmacy_enable():
    data = request.get_json()
    pharmid = data.get('pharmid')
    counter = PharmacyInfo.query.filter_by(pharmid=pharmid).first()
    if not counter:
        return jsonify({'error': 'Pharmacy counter not found'}), 404
    counter.status = True
    db.session.commit()
    return jsonify({'status': 'enabled', 'pharmid': pharmid})


@app.route('/api/pharmacy/disable', methods=['POST'])
def pharmacy_disable():
    data = request.get_json()
    pharmid = data.get('pharmid')
    counter = PharmacyInfo.query.filter_by(pharmid=pharmid).first()
    if not counter:
        return jsonify({'error': 'Pharmacy counter not found'}), 404

    queue = []
    if counter.queueinfo:
        try:
            queue = json.loads(counter.queueinfo)
        except Exception:
            queue = []
    else:
        queue = []

    if queue:
        return jsonify({'status': 'disabling', 'pharmid': pharmid, 'message': 'Cannot disable while queue is not empty.'})
    else:
        counter.status = False
        db.session.commit()
        return jsonify({'status': 'disabled', 'pharmid': pharmid})





if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
