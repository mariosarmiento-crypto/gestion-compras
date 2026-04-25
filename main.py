import os
from datetime import datetime, timedelta, date
from io import BytesIO
from typing import Optional, List

import bcrypt
import jwt
import pandas as pd
from fastapi import FastAPI, Depends, HTTPException, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import Column, Date, Float, ForeignKey, Integer, String, UniqueConstraint, create_engine, text
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

SECRET_KEY = os.getenv("SECRET_KEY", "CAMBIAR_ESTA_CLAVE")
ALGORITHM = "HS256"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./gastos.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


class Hotel(Base):
    __tablename__ = "hoteles"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, index=True)


class User(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String, default="operador")
    hotel_scope = Column(String, default="ALL")  # ALL, Mare Hotel, Reñaca House


class Provider(Base):
    __tablename__ = "proveedores"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, index=True)
    rut = Column(String, unique=True, index=True)


class Movement(Base):
    __tablename__ = "movimientos"
    id = Column(Integer, primary_key=True)
    hotel_id = Column(Integer, ForeignKey("hoteles.id"))
    fecha = Column(Date)
    descripcion = Column(String)
    nombre_cuenta = Column(String)
    provider_id = Column(Integer, ForeignKey("proveedores.id"))
    tipo_documento = Column(String)
    numero_documento = Column(String)
    monto_total = Column(Float)
    iva = Column(Float)
    neto = Column(Float)
    observaciones = Column(String, nullable=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"))
    hotel = relationship("Hotel")
    provider = relationship("Provider")
    user = relationship("User")
    __table_args__ = (
        UniqueConstraint("provider_id", "tipo_documento", "numero_documento", name="uq_doc_proveedor"),
    )


class HotelOut(BaseModel):
    id: int
    name: str

    class Config:
        from_attributes = True


class ProviderOut(BaseModel):
    id: int
    name: str
    rut: str

    class Config:
        from_attributes = True


class MovementCreate(BaseModel):
    hotel_id: int
    fecha: date
    descripcion: str
    nombre_cuenta: str
    proveedor: str
    rut: str
    tipo_documento: str
    numero_documento: str
    monto_total: float
    iva: float
    neto: float
    observaciones: Optional[str] = ""


class MovementOut(BaseModel):
    id: int
    hotel_id: int
    fecha: date
    descripcion: str
    nombre_cuenta: str
    tipo_documento: str
    numero_documento: str
    monto_total: float
    iva: float
    neto: float
    observaciones: Optional[str]
    hotel: HotelOut
    provider: ProviderOut

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def hp(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def cp(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def mt(username: str) -> str:
    return jwt.encode(
        {"sub": username, "exp": datetime.utcnow() + timedelta(days=1)},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def user_from_token(token: str, db: Session):
    try:
        username = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub")
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")
    return user


def current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(db_session)):
    return user_from_token(token, db)


def allowed_hotels(db: Session, user: User):
    query = db.query(Hotel)
    if user.hotel_scope == "ALL":
        return query.order_by(Hotel.name).all()
    return query.filter(Hotel.name == user.hotel_scope).order_by(Hotel.name).all()


def allowed_ids(db: Session, user: User):
    return [hotel.id for hotel in allowed_hotels(db, user)]


def ensure_access(db: Session, user: User, hotel_id: int):
    if hotel_id not in allowed_ids(db, user):
        raise HTTPException(status_code=403, detail="Usuario sin acceso a este hotel")


def get_or_create_provider(db: Session, name: str, rut: str) -> Provider:
    provider = db.query(Provider).filter(Provider.name == name).first()
    if provider and provider.rut != rut:
        raise HTTPException(status_code=400, detail=f"Proveedor existente con otro RUT: {provider.rut}")
    if not provider:
        by_rut = db.query(Provider).filter(Provider.rut == rut).first()
        if by_rut:
            raise HTTPException(status_code=400, detail=f"El RUT ya existe para proveedor: {by_rut.name}")
        provider = Provider(name=name, rut=rut)
        db.add(provider)
        db.commit()
        db.refresh(provider)
    return provider


def get_or_create_provider_for_edit(db: Session, name: str, rut: str) -> Provider:
    name = (name or "").strip()
    rut = (rut or "").strip()
    if not name or not rut:
        raise HTTPException(status_code=400, detail="Proveedor y RUT son obligatorios")

    provider_by_name = db.query(Provider).filter(Provider.name == name).first()
    provider_by_rut = db.query(Provider).filter(Provider.rut == rut).first()

    if provider_by_name and provider_by_name.rut == rut:
        return provider_by_name

    if provider_by_rut:
        if provider_by_rut.name != name and not provider_by_name:
            provider_by_rut.name = name
            db.commit()
            db.refresh(provider_by_rut)
        return provider_by_rut

    if provider_by_name:
        provider_by_name.rut = rut
        db.commit()
        db.refresh(provider_by_name)
        return provider_by_name

    provider = Provider(name=name, rut=rut)
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


app = FastAPI(title="Gestión de Compras y Gastos")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
Base.metadata.create_all(bind=engine)


def ensure_sqlite_columns():
    if not DATABASE_URL.startswith("sqlite"):
        return
    with engine.connect() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(usuarios)"))]
        if "hotel_scope" not in cols:
            conn.execute(text("ALTER TABLE usuarios ADD COLUMN hotel_scope VARCHAR DEFAULT 'ALL'"))
        conn.commit()


ensure_sqlite_columns()


@app.on_event("startup")
def seed():
    db = SessionLocal()
    try:
        for hotel_name in ["Mare Hotel", "Reñaca House"]:
            if not db.query(Hotel).filter(Hotel.name == hotel_name).first():
                db.add(Hotel(name=hotel_name))
        users = [
            ("admin", "admin123", "admin", "ALL"),
            ("mare", "mare123", "operador", "Mare Hotel"),
            ("renaca", "renaca123", "operador", "Reñaca House"),
        ]
        for username, password, role, scope in users:
            user = db.query(User).filter(User.username == username).first()
            if not user:
                db.add(User(username=username, hashed_password=hp(password), role=role, hotel_scope=scope))
            else:
                user.role = role
                user.hotel_scope = scope
        db.commit()
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML_PAGE)


@app.post("/token", response_model=Token)
def token(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(db_session)):
    user = db.query(User).filter(User.username == form.username).first()
    if not user or not cp(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Usuario o clave incorrecta")
    return {"access_token": mt(user.username), "token_type": "bearer"}


@app.get("/me")
def me(db: Session = Depends(db_session), user: User = Depends(current_user)):
    return {"username": user.username, "role": user.role, "scope": user.hotel_scope}


@app.get("/hoteles", response_model=List[HotelOut])
def hoteles(db: Session = Depends(db_session), user: User = Depends(current_user)):
    return allowed_hotels(db, user)


@app.get("/proveedores", response_model=List[ProviderOut])
def proveedores(db: Session = Depends(db_session), user: User = Depends(current_user)):
    return db.query(Provider).order_by(Provider.name).all()


@app.get("/cuentas")
def cuentas(db: Session = Depends(db_session), user: User = Depends(current_user)):
    return [
        row[0]
        for row in db.query(Movement.nombre_cuenta)
        .filter(Movement.hotel_id.in_(allowed_ids(db, user)))
        .distinct()
        .all()
        if row[0]
    ]


@app.post("/movimientos", response_model=MovementOut)
def crear(movement: MovementCreate, db: Session = Depends(db_session), user: User = Depends(current_user)):
    ensure_access(db, user, movement.hotel_id)
    provider = get_or_create_provider(db, movement.proveedor, movement.rut)
    mov = Movement(
        hotel_id=movement.hotel_id,
        fecha=movement.fecha,
        descripcion=movement.descripcion,
        nombre_cuenta=movement.nombre_cuenta,
        provider_id=provider.id,
        tipo_documento=movement.tipo_documento,
        numero_documento=movement.numero_documento,
        monto_total=movement.monto_total,
        iva=movement.iva,
        neto=movement.neto,
        observaciones=movement.observaciones,
        user_id=user.id,
    )
    try:
        db.add(mov)
        db.commit()
        db.refresh(mov)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=400, detail="Registro duplicado: proveedor + tipo documento + número documento")
    return mov


@app.put("/movimientos/{movement_id}", response_model=MovementOut)
def editar(movement_id: int, movement: MovementCreate, db: Session = Depends(db_session), user: User = Depends(current_user)):
    mov = db.query(Movement).filter(Movement.id == movement_id).first()
    if not mov:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    ensure_access(db, user, mov.hotel_id)
    ensure_access(db, user, movement.hotel_id)
    provider = get_or_create_provider_for_edit(db, movement.proveedor, movement.rut)
    mov.hotel_id = movement.hotel_id
    mov.fecha = movement.fecha
    mov.descripcion = movement.descripcion
    mov.nombre_cuenta = movement.nombre_cuenta
    mov.provider_id = provider.id
    mov.tipo_documento = movement.tipo_documento
    mov.numero_documento = movement.numero_documento
    mov.monto_total = movement.monto_total
    mov.iva = movement.iva
    mov.neto = movement.neto
    mov.observaciones = movement.observaciones
    try:
        db.commit()
        db.refresh(mov)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=400, detail="No se pudo editar. Revise duplicados de documento/proveedor")
    return mov


@app.get("/movimientos", response_model=List[MovementOut])
def listar(
    hotel_id: Optional[int] = None,
    fecha_desde: Optional[date] = None,
    fecha_hasta: Optional[date] = None,
    numero_documento: Optional[str] = None,
    db: Session = Depends(db_session),
    user: User = Depends(current_user),
):
    query = db.query(Movement).filter(Movement.hotel_id.in_(allowed_ids(db, user)))
    if hotel_id:
        ensure_access(db, user, hotel_id)
        query = query.filter(Movement.hotel_id == hotel_id)
    if fecha_desde:
        query = query.filter(Movement.fecha >= fecha_desde)
    if fecha_hasta:
        query = query.filter(Movement.fecha <= fecha_hasta)
    if numero_documento:
        query = query.filter(Movement.numero_documento.contains(numero_documento))
    return query.order_by(Movement.fecha.desc(), Movement.id.desc()).all()


@app.delete("/movimientos/{movement_id}")
def eliminar(movement_id: int, db: Session = Depends(db_session), user: User = Depends(current_user)):
    mov = db.query(Movement).filter(Movement.id == movement_id).first()
    if not mov:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    ensure_access(db, user, mov.hotel_id)
    db.delete(mov)
    db.commit()
    return {"ok": True}


def export_rows(db: Session, user: User, hotel_id=None, fecha_desde=None, fecha_hasta=None):
    query = db.query(Movement).filter(Movement.hotel_id.in_(allowed_ids(db, user)))
    if hotel_id:
        ensure_access(db, user, hotel_id)
        query = query.filter(Movement.hotel_id == hotel_id)
    if fecha_desde:
        query = query.filter(Movement.fecha >= fecha_desde)
    if fecha_hasta:
        query = query.filter(Movement.fecha <= fecha_hasta)
    rows = []
    for m in query.order_by(Movement.fecha.desc()).all():
        rows.append({
            "FECHA": m.fecha,
            "HOTEL": m.hotel.name,
            "DESCRIPCION": m.descripcion,
            "NOMBRE DE CUENTA": m.nombre_cuenta,
            "PROVEEDOR": m.provider.name,
            "RUT": m.provider.rut,
            "TIPO DE DOCUMENTO": m.tipo_documento,
            "NUMERO DOC.": m.numero_documento,
            "MONTO TOTAL": m.monto_total,
            "IVA": m.iva,
            "NETO": m.neto,
            "OBSERVACIONES": m.observaciones or "",
            "USUARIO": m.user.username if m.user else "",
        })
    return rows


@app.get("/export/{fmt}")
def exportar(
    fmt: str,
    token: str = Query(...),
    hotel_id: Optional[int] = None,
    fecha_desde: Optional[date] = None,
    fecha_hasta: Optional[date] = None,
    db: Session = Depends(db_session),
):
    user = user_from_token(token, db)
    rows = export_rows(db, user, hotel_id, fecha_desde, fecha_hasta)
    if fmt == "excel":
        out = BytesIO()
        pd.DataFrame(rows).to_excel(out, index=False, sheet_name="Registros", engine="openpyxl")
        out.seek(0)
        return Response(
            out.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=registros.xlsx"},
        )
    if fmt == "pdf":
        out = BytesIO()
        doc = SimpleDocTemplate(out, pagesize=landscape(letter))
        styles = getSampleStyleSheet()
        elements = [Paragraph("Registro de compras y gastos", styles["Title"])]
        if rows:
            headers = list(rows[0].keys())
            data = [headers] + [[str(row.get(h, "")) for h in headers] for row in rows]
            table = Table(data, repeatRows=1)
            table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
            ]))
            elements.append(table)
        else:
            elements.append(Paragraph("No hay datos para mostrar.", styles["Normal"]))
        doc.build(elements)
        out.seek(0)
        return Response(out.getvalue(), media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=registros.pdf"})
    raise HTTPException(status_code=400, detail="Formato no soportado")


HTML_PAGE = r'''
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gestión de Compras y Gastos</title>
<style>
body{font-family:Arial,sans-serif;margin:0;background:#f5f6f8;color:#222}.wrap{max-width:1100px;margin:auto;padding:18px}.card{background:#fff;border-radius:14px;padding:18px;box-shadow:0 2px 12px #0001;margin-bottom:16px}input,select,button{font-size:15px;padding:10px;border:1px solid #ccc;border-radius:8px;box-sizing:border-box}button{background:#111;color:#fff;cursor:pointer;border:0}.secondary{background:#555}.danger{background:#b42318}.edit{background:#1769aa}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.hidden{display:none}table{width:100%;border-collapse:collapse;background:white}th,td{padding:8px;border-bottom:1px solid #ddd;text-align:left;font-size:13px}.top{display:flex;justify-content:space-between;gap:10px;align-items:center}.actions{display:flex;gap:8px;flex-wrap:wrap}.muted{color:#777;font-size:13px}@media(max-width:700px){.top{display:block}.actions{margin-top:10px}table{display:block;overflow:auto}.wrap{padding:10px}.card{padding:12px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card top">
    <div><h2>Gestión de Compras y Gastos</h2><div class="muted">Mare Hotel / Reñaca House · <span id="userInfo"></span></div></div>
    <div class="actions"><button type="button" onclick="showForm()">+ Nuevo registro</button><button type="button" class="secondary" onclick="logout()">Salir</button></div>
  </div>
  <div id="loginCard" class="card">
    <h3>Ingreso</h3>
    <p class="muted">admin/admin123 · mare/mare123 · renaca/renaca123</p>
    <div class="grid"><input id="username" placeholder="Usuario" value="admin"><input id="password" type="password" placeholder="Clave" value="admin123"><button type="button" onclick="doLogin()">Entrar</button></div>
  </div>
  <div id="appPanel" class="hidden">
    <div id="formCard" class="card hidden">
      <h3 id="formTitle">Nuevo registro</h3>
      <div class="grid">
        <select id="hotel_id"></select>
        <input id="fecha" type="date">
        <input id="descripcion" placeholder="Descripción / Glosa">
        <input id="nombre_cuenta" list="accountsList" placeholder="Nombre de cuenta"><datalist id="accountsList"></datalist>
        <input id="proveedor" list="providersList" placeholder="Proveedor"><datalist id="providersList"></datalist>
        <input id="rut" placeholder="RUT">
        <select id="tipo_documento"><option>Factura</option><option>Boleta</option><option>Factura exenta</option><option>Nota de crédito</option><option>Nota de débito</option><option>Otro</option></select>
        <input id="numero_documento" placeholder="Número doc.">
        <input id="monto_total" type="number" step="0.01" placeholder="Monto total">
        <input id="iva" type="number" step="0.01" placeholder="IVA">
        <input id="neto" type="number" step="0.01" placeholder="Neto">
        <input id="observaciones" placeholder="Observaciones">
      </div><br>
      <button type="button" onclick="saveMovement()" id="saveBtn">Guardar</button>
      <button type="button" class="secondary" onclick="cancelEdit()">Cancelar</button>
    </div>
    <div class="card"><h3>Filtros</h3>
      <div class="grid"><select id="filter_hotel"><option value="">Todos los hoteles</option></select><input id="fecha_desde" type="date"><input id="fecha_hasta" type="date"><input id="doc_filter" placeholder="Nº documento"><button type="button" onclick="loadMovements()">Filtrar</button></div><br>
      <div class="actions"><button type="button" class="secondary" onclick="exportFile('excel')">Descargar Excel</button><button type="button" class="secondary" onclick="exportFile('pdf')">Descargar PDF</button></div>
    </div>
    <div class="card"><h3>Registros</h3><div style="overflow:auto"><table><thead><tr><th>Fecha</th><th>Hotel</th><th>Cuenta</th><th>Proveedor</th><th>RUT</th><th>Doc</th><th>Total</th><th>IVA</th><th>Neto</th><th>Acciones</th></tr></thead><tbody id="rows"></tbody></table></div></div>
  </div>
</div>
<script>
let token=localStorage.getItem('token')||'';let providers=[];let movements=[];let editingId=null;let editMode=false;
function el(id){return document.getElementById(id)}
function headers(){return {'Authorization':'Bearer '+token,'Content-Type':'application/json'}}
async function doLogin(){let body=new URLSearchParams();body.append('username',el('username').value);body.append('password',el('password').value);let r=await fetch('/token',{method:'POST',body:body});if(!r.ok){alert('Usuario o clave incorrecta');return}let j=await r.json();token=j.access_token;localStorage.setItem('token',token);init()}
function logout(){localStorage.removeItem('token');location.reload()}
function showForm(){editingId=null;editMode=false;el('formTitle').textContent='Nuevo registro';el('saveBtn').textContent='Guardar';clearForm();el('formCard').classList.remove('hidden');el('fecha').value=new Date().toISOString().slice(0,10)}
function hideForm(){el('formCard').classList.add('hidden')}
function cancelEdit(){editingId=null;editMode=false;hideForm();clearForm()}
function clearForm(){document.querySelectorAll('#formCard input').forEach(i=>{i.value='';i.readOnly=false;i.style.background='#fff'});}
async function init(){if(!token)return;el('loginCard').classList.add('hidden');el('appPanel').classList.remove('hidden');let me=await (await fetch('/me',{headers:headers()})).json();el('userInfo').textContent=me.username+' ('+me.scope+')';await loadHotels();await loadProviders();await loadAccounts();await loadMovements()}
async function loadHotels(){let r=await fetch('/hoteles',{headers:headers()});if(!r.ok){logout();return}let data=await r.json();el('hotel_id').innerHTML='';el('filter_hotel').innerHTML='<option value="">Todos los hoteles</option>';data.forEach(h=>{el('hotel_id').innerHTML+=`<option value="${h.id}">${h.name}</option>`;el('filter_hotel').innerHTML+=`<option value="${h.id}">${h.name}</option>`})}
async function loadProviders(){let r=await fetch('/proveedores',{headers:headers()});providers=await r.json();el('providersList').innerHTML='';providers.forEach(p=>el('providersList').innerHTML+=`<option value="${p.name}"></option>`)}
el('proveedor').addEventListener('input',()=>{let nombre=el('proveedor').value.trim().toLowerCase();let p=providers.find(x=>x.name.toLowerCase()===nombre);if(p){el('rut').value=p.rut;if(!editMode){el('rut').readOnly=true;el('rut').style.background='#eee'}else{el('rut').readOnly=false;el('rut').style.background='#fff'}}else{el('rut').readOnly=false;el('rut').style.background='#fff'}});
async function loadAccounts(){let r=await fetch('/cuentas',{headers:headers()});let data=await r.json();el('accountsList').innerHTML='';data.forEach(c=>el('accountsList').innerHTML+=`<option value="${c}"></option>`)}
function formData(){return {hotel_id:+el('hotel_id').value,fecha:el('fecha').value,descripcion:el('descripcion').value,nombre_cuenta:el('nombre_cuenta').value,proveedor:el('proveedor').value,rut:el('rut').value,tipo_documento:el('tipo_documento').value,numero_documento:el('numero_documento').value,monto_total:+el('monto_total').value||0,iva:+el('iva').value||0,neto:+el('neto').value||0,observaciones:el('observaciones').value}}
async function saveMovement(){let method=editingId?'PUT':'POST';let url=editingId?'/movimientos/'+editingId:'/movimientos';let r=await fetch(url,{method:method,headers:headers(),body:JSON.stringify(formData())});if(!r.ok){let e=await r.json();alert(e.detail||'Error al guardar');return}editingId=null;editMode=false;hideForm();clearForm();await loadProviders();await loadAccounts();await loadMovements()}
function editMov(id){let m=movements.find(x=>x.id===id);if(!m)return;editingId=id;editMode=true;el('formTitle').textContent='Editar registro';el('saveBtn').textContent='Guardar cambios';el('hotel_id').value=m.hotel_id;el('fecha').value=m.fecha;el('descripcion').value=m.descripcion;el('nombre_cuenta').value=m.nombre_cuenta;el('proveedor').value=m.provider.name;el('rut').value=m.provider.rut;el('tipo_documento').value=m.tipo_documento;el('numero_documento').value=m.numero_documento;el('monto_total').value=m.monto_total;el('iva').value=m.iva;el('neto').value=m.neto;el('observaciones').value=m.observaciones||'';el('rut').readOnly=false;el('rut').style.background='#fff';el('formCard').classList.remove('hidden');window.scrollTo({top:0,behavior:'smooth'})}
async function loadMovements(){let p=new URLSearchParams();if(el('filter_hotel').value)p.append('hotel_id',el('filter_hotel').value);if(el('fecha_desde').value)p.append('fecha_desde',el('fecha_desde').value);if(el('fecha_hasta').value)p.append('fecha_hasta',el('fecha_hasta').value);if(el('doc_filter').value)p.append('numero_documento',el('doc_filter').value);let r=await fetch('/movimientos?'+p.toString(),{headers:headers()});if(!r.ok){logout();return}movements=await r.json();el('rows').innerHTML='';movements.forEach(m=>el('rows').innerHTML+=`<tr><td>${m.fecha}</td><td>${m.hotel.name}</td><td>${m.nombre_cuenta}</td><td>${m.provider.name}</td><td>${m.provider.rut}</td><td>${m.tipo_documento} ${m.numero_documento}</td><td>${m.monto_total}</td><td>${m.iva}</td><td>${m.neto}</td><td><button type="button" class="edit" onclick="editMov(${m.id})">Editar</button> <button type="button" class="danger" onclick="delMov(${m.id})">X</button></td></tr>`)}
async function delMov(id){if(!confirm('¿Eliminar registro?'))return;await fetch('/movimientos/'+id,{method:'DELETE',headers:headers()});loadMovements()}
function exportFile(fmt){let p=new URLSearchParams();p.append('token',token);if(el('filter_hotel').value)p.append('hotel_id',el('filter_hotel').value);if(el('fecha_desde').value)p.append('fecha_desde',el('fecha_desde').value);if(el('fecha_hasta').value)p.append('fecha_hasta',el('fecha_hasta').value);location.href='/export/'+fmt+'?'+p.toString()}
init();
</script>
</body>
</html>
'''
