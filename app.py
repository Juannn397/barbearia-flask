
import io
import os
import secrets
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import (
    Flask, flash, jsonify, redirect, render_template,
    request, session, url_for
)

from PIL import Image, ImageOps, UnidentifiedImageError
from supabase import create_client
from werkzeug.exceptions import RequestEntityTooLarge

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.getenv("VERCEL"))

db = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SECRET_KEY"]
)

BUCKET = "fotos-barbearia"
FUSO = ZoneInfo("America/Sao_Paulo")

SERVICOS = {
    "Corte tradicional": 35,
    "Corte degradê": 45,
    "Barba": 25,
    "Corte + barba": 60,
    "Corte na tesoura": 50
}

BARBEIROS = ["Rafael", "Lucas", "André"]

HORARIOS = []
hora = datetime.strptime("09:00", "%H:%M")
fim = datetime.strptime("18:00", "%H:%M")

while hora < fim:
    HORARIOS.append(hora.strftime("%H:%M"))
    hora += timedelta(minutes=30)


def hoje():
    return datetime.now(FUSO).date()


# Proteção dos formulários
@app.before_request
def proteger_formularios():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(32)

    if request.method == "POST":
        recebido = request.form.get("_csrf", "")
        esperado = session["_csrf"]

        if not secrets.compare_digest(recebido, esperado):
            return "Formulário inválido. Atualize a página.", 400


@app.errorhandler(RequestEntityTooLarge)
def arquivo_grande(erro):
    return "Foto muito grande. O limite é 5 MB.", 413


# Página pública para os clientes
@app.route("/")
def inicio():
    return render_template(
        "index.html",
        servicos=SERVICOS,
        barbeiros=BARBEIROS,
        horarios=HORARIOS,
        hoje=hoje().isoformat(),
        csrf=session["_csrf"]
    )


# Consultar horarios livres no Supabase
@app.route("/horarios-disponiveis")
def horarios_disponiveis():
    barbeiro = request.args.get("barbeiro", "")
    data = request.args.get("data", "")

    if barbeiro not in BARBEIROS:
        return jsonify({"horarios": []}), 400

    try:
        data_escolhida = datetime.strptime(
            data, "%Y-%m-%d"
        ).date()
    except ValueError:
        return jsonify({"horarios": []}), 400

    agora = datetime.now(FUSO)

    if data_escolhida < agora.date():
        return jsonify({"horarios": []})

    resultado = (
        db.table("agendamentos_barbearia")
        .select("horario")
        .eq("barbeiro", barbeiro)
        .eq("data", data)
        .execute()
    )

    ocupados = {
        str(item["horario"])[:5]
        for item in resultado.data
    }

    livres = []

    for horario in HORARIOS:
        momento = datetime.strptime(
            f"{data} {horario}",
            "%Y-%m-%d %H:%M"
        ).replace(tzinfo=FUSO)

        if horario not in ocupados and momento > agora:
            livres.append(horario)

    return jsonify({"horarios": livres})


# Cadastrar o agendamento
@app.route("/agendar", methods=["POST"])
def agendar():
    cliente = request.form.get("cliente", "").strip()
    servico = request.form.get("servico", "")
    barbeiro = request.form.get("barbeiro", "")
    data = request.form.get("data", "")
    horario = request.form.get("horario", "")

    if not cliente or len(cliente) > 100:
        flash("Informe um nome válido.")
        return redirect(url_for("inicio"))

    if servico not in SERVICOS or barbeiro not in BARBEIROS:
        flash("Serviço ou barbeiro inválido.")
        return redirect(url_for("inicio"))

    try:
        data_escolhida = datetime.strptime(
            data, "%Y-%m-%d"
        ).date()
    except ValueError:
        flash("Data inválida.")
        return redirect(url_for("inicio"))

    if horario not in HORARIOS:
        flash("Escolha um horário válido.")
        return redirect(url_for("inicio"))

    momento_agendamento = datetime.strptime(
        f"{data} {horario}",
        "%Y-%m-%d %H:%M"
    ).replace(tzinfo=FUSO)

    if momento_agendamento <= datetime.now(FUSO):
        flash("Não é possível agendar um horário que já passou.")
        return redirect(url_for("inicio"))

    try:
        db.table("agendamentos_barbearia").insert({
            "cliente": cliente,
            "servico": servico,
            "barbeiro": barbeiro,
            "data": data,
            "horario": horario,
            "status": "agendado"
        }).execute()

        flash("Agendamento realizado com sucesso!")

    except Exception as erro:
        if getattr(erro, "code", None) == "23505":
            flash("Esse barbeiro já possui um agendamento nesse horário.")
        else:
            app.logger.exception("Falha ao agendar")
            flash("Não foi possível salvar. Tente novamente.")

    return redirect(url_for("inicio"))


# Painel administrativo e login
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        senha = request.form.get("senha", "")

        if secrets.compare_digest(
            senha, os.environ["ADMIN_PASSWORD"]
        ):
            session["admin"] = True
            return redirect(url_for("admin"))

        flash("Senha incorreta.")
        return redirect(url_for("admin"))

    agendamentos = []

    if session.get("admin"):
        resposta = (
            db.table("agendamentos_barbearia")
            .select("*")
            .order("data", desc=True)
            .order("horario")
            .range(0, 999)
            .execute()
        )

        agendamentos = resposta.data

        # Links temporários para fotos privadas
        for item in agendamentos:
            item["foto_url"] = None

            if item.get("foto_path"):
                try:
                    resultado = (
                        db.storage.from_(BUCKET)
                        .create_signed_url(
                            item["foto_path"], 600
                        )
                    )

                    item["foto_url"] = (
                        resultado.get("signedURL")
                        or resultado.get("signedUrl")
                    )
                except Exception:
                    app.logger.exception("Falha ao gerar URL da foto")

    return render_template(
        "admin.html",
        autenticado=session.get("admin", False),
        agendamentos=agendamentos,
        csrf=session["_csrf"]
    )


# Enviar uma foto e concluir o atendimento
@app.route("/concluir/<int:agendamento_id>", methods=["POST"])
def concluir(agendamento_id):
    if not session.get("admin"):
        return redirect(url_for("admin"))

    resultado = (
        db.table("agendamentos_barbearia")
        .select("*")
        .eq("id", agendamento_id)
        .limit(1)
        .execute()
    )

    if not resultado.data:
        flash("Agendamento não encontrado.")
        return redirect(url_for("admin"))

    agendamento = resultado.data[0]

    if agendamento["status"] == "concluido":
        flash("Este agendamento já foi concluído.")
        return redirect(url_for("admin"))

    arquivo = request.files.get("foto")

    if not arquivo or not arquivo.filename:
        flash("Selecione uma fotografia.")
        return redirect(url_for("admin"))

    if not arquivo.filename.lower().endswith(
        (".jpg", ".jpeg", ".png", ".webp")
    ):
        flash("Envie uma imagem JPG, PNG ou WEBP.")
        return redirect(url_for("admin"))

    try:
        # Verificar se os bytes são realmente uma imagem
        conteudo = arquivo.read()

        with Image.open(io.BytesIO(conteudo)) as imagem:
            if imagem.format not in ("JPEG", "PNG", "WEBP"):
                raise ValueError("Formato inválido")

            imagem.verify()

        # Padronizar como JPEG e remover metadados
        with Image.open(io.BytesIO(conteudo)) as imagem:
            imagem = ImageOps.exif_transpose(imagem)
            imagem = imagem.convert("RGB")
            imagem.thumbnail((2000, 2000))

            buffer = io.BytesIO()
            imagem.save(buffer, format="JPEG", quality=85)
            foto_bytes = buffer.getvalue()

    except (UnidentifiedImageError, OSError, ValueError):
        flash("O arquivo não é uma fotografia válida.")
        return redirect(url_for("admin"))

    caminho = f"agendamento-{agendamento_id}/{uuid.uuid4().hex}.jpg"

    try:
        # Primeiro: salvar a fotografia
        db.storage.from_(BUCKET).upload(
            path=caminho,
            file=foto_bytes,
            file_options={
                "content-type": "image/jpeg",
                "upsert": "false"
            }
        )

    except Exception:
        app.logger.exception("Falha ao enviar fotografia")
        flash("Não foi possível salvar a foto. Atendimento pendente.")
        return redirect(url_for("admin"))

    try:
        # Segundo: atualizar o agendamento
        resposta = (
            db.table("agendamentos_barbearia")
            .update({
                "foto_path": caminho,
                "status": "concluido"
            })
            .eq("id", agendamento_id)
            .eq("status", "agendado")
            .execute()
        )

        if not resposta.data:
            raise ValueError("Agendamento não estava pendente.")

        flash("Foto enviada! Atendimento concluído.")

    except Exception:
        app.logger.exception("Falha ao concluir agendamento")

        # Tentar remover a foto se o banco não foi atualizado
        try:
            db.storage.from_(BUCKET).remove([caminho])
        except Exception:
            app.logger.exception("Falha ao limpar foto não vinculada")

        flash("Não foi possível concluir o atendimento.")

    return redirect(url_for("admin"))


@app.route("/sair", methods=["POST"])
def sair():
    session.pop("admin", None)
    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(debug=True)
