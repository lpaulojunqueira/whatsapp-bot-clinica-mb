"""
Servidor da Ana — atendimento por IA no WhatsApp.

Recebe os webhooks do WhatsApp Cloud API, identifica de QUAL clínica é,
processa a mensagem em segundo plano (com delay natural e indicador de
digitação), e responde via Claude.
"""

from flask import Flask, request, jsonify
import os
import requests
import threading
import time
import random
import re

from db import (
    inicializar_banco,
    buscar_clinica_por_phone_id,
    obter_ou_criar_conversa,
    mensagem_ja_processada,
    salvar_mensagem,
    obter_historico_conversa,
    conversa_esta_pausada,
)
from painel import registrar_rotas as registrar_rotas_painel

app = Flask(__name__)

# Chave de assinatura dos cookies de sessão (login do painel).
# Se SECRET_KEY não estiver definida, usa um valor padrão (NÃO seguro pra produção).
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "troque-essa-chave-em-producao")

# ============================================================
# CONFIGURAÇÕES
# ============================================================
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "meu_token_secreto_123")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
WHATSAPP_API_URL = "https://graph.facebook.com/v21.0"
OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"

# Ritmo de "digitação" simulada
TEMPO_POR_CARACTERE = 0.045   # ~45ms por caractere = ritmo natural
DELAY_MINIMO = 3.0            # nunca menos que 3s
DELAY_MAXIMO = 8.0            # nunca mais que 8s


# ============================================================
# WHATSAPP API
# ============================================================
def marcar_como_lida_e_digitando(phone_number_id, whatsapp_message_id):
    """
    Marca a mensagem do lead como lida (✓✓ azul) E mostra "Ana está digitando...".
    Faz isso em uma única chamada à API da Meta.
    """
    url = f"{WHATSAPP_API_URL}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": whatsapp_message_id,
        "typing_indicator": {"type": "text"},
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️  Erro ao marcar lida/digitando: {e}")


def enviar_mensagem_whatsapp(phone_number_id, numero_destino, texto):
    """Envia uma mensagem de texto para o lead."""
    url = f"{WHATSAPP_API_URL}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "text",
        "text": {"body": texto},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"❌ Erro ao enviar mensagem: {e}")
        if "r" in locals():
            print(f"   Resposta da Meta: {r.text}")
        return False


# ============================================================
# IA (Claude)
# ============================================================
def gerar_resposta_ia(system_prompt, historico, mensagem_atual):
    """Chama a API do Claude e retorna o texto da resposta (ou None em erro)."""
    messages = list(historico)
    messages.append({"role": "user", "content": mensagem_atual})

    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 400,
        "temperature": 0.7,
        "system": system_prompt,
        "messages": messages,
    }

    try:
        r = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=30)
        if r.status_code != 200:
            print(f"❌ Claude retornou {r.status_code}: {r.text}")
            return None
        data = r.json()
        return data["content"][0]["text"].strip()
    except Exception as e:
        print(f"❌ Erro ao chamar Claude: {e}")
        return None


# ============================================================
# ÁUDIO (download do WhatsApp + transcrição via OpenAI Whisper)
# ============================================================
def baixar_audio_whatsapp(media_id):
    """
    Baixa o arquivo de áudio do WhatsApp em duas etapas:
    1) pede a URL temporária do arquivo (autenticada)
    2) baixa o conteúdo binário dessa URL
    Retorna os bytes do áudio, ou None em caso de erro.
    """
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        # Etapa 1: pega a URL temporária
        r1 = requests.get(
            f"{WHATSAPP_API_URL}/{media_id}",
            headers=headers, timeout=10
        )
        r1.raise_for_status()
        url_arquivo = r1.json().get("url")
        if not url_arquivo:
            print("⚠️  WhatsApp não retornou URL do áudio.")
            return None

        # Etapa 2: baixa o conteúdo
        r2 = requests.get(url_arquivo, headers=headers, timeout=30)
        r2.raise_for_status()
        return r2.content
    except Exception as e:
        print(f"❌ Erro ao baixar áudio: {e}")
        return None


def transcrever_audio(audio_bytes):
    """
    Manda o áudio pra OpenAI Whisper e retorna o texto transcrito.
    Usa language='pt' pra otimizar pra português.
    """
    if not audio_bytes:
        return None

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    # WhatsApp manda áudio em OGG/Opus — Whisper aceita direto.
    files = {"file": ("audio.ogg", audio_bytes, "audio/ogg")}
    data = {"model": "whisper-1", "language": "pt"}

    try:
        r = requests.post(
            OPENAI_TRANSCRIBE_URL,
            headers=headers, files=files, data=data, timeout=60
        )
        if r.status_code != 200:
            print(f"❌ Whisper retornou {r.status_code}: {r.text}")
            return None
        texto = r.json().get("text", "").strip()
        return texto if texto else None
    except Exception as e:
        print(f"❌ Erro ao transcrever áudio: {e}")
        return None


# ============================================================
# QUEBRA DE RESPOSTA EM PARTES
# ============================================================
def quebrar_em_partes(texto):
    """
    Quebra a resposta em mensagens curtas — como uma pessoa real no WhatsApp.
    Limita a no máximo 3 partes pra não virar metralhadora.
    """
    texto = texto.strip()

    # Resposta curta vai inteira.
    if len(texto) <= 120:
        return [texto]

    # Se a Ana usou quebra de linha propositalmente, respeitamos.
    partes_por_linha = [p.strip() for p in texto.split("\n") if p.strip()]
    if len(partes_por_linha) >= 2:
        return partes_por_linha[:3]

    # Quebra por frases (.!?).
    frases = re.split(r"(?<=[.!?])\s+", texto)
    frases = [f.strip() for f in frases if f.strip()]
    if len(frases) <= 1:
        return [texto]

    # Agrupa frases em blocos de ~120 caracteres.
    partes = []
    parte_atual = ""
    for frase in frases:
        if parte_atual and len(parte_atual) + len(frase) + 1 > 120:
            partes.append(parte_atual.strip())
            parte_atual = frase
        else:
            parte_atual = (parte_atual + " " + frase).strip()
    if parte_atual:
        partes.append(parte_atual.strip())

    # Limita a 3 partes (junta o que sobrar na última).
    if len(partes) > 3:
        partes = partes[:2] + [" ".join(partes[2:])]

    return partes


def calcular_delay(texto):
    """Tempo proporcional ao tamanho da resposta, com leve aleatoriedade."""
    base = len(texto) * TEMPO_POR_CARACTERE
    base += random.uniform(0.5, 1.5)
    return max(DELAY_MINIMO, min(DELAY_MAXIMO, base))


# ============================================================
# PROCESSAMENTO EM SEGUNDO PLANO
# ============================================================
def processar_mensagem_em_background(
    clinica, conversa_id, message_id_whatsapp, numero_lead,
    texto_recebido=None, audio_media_id=None
):
    """
    Roda em uma thread separada — o webhook já respondeu 200 pra Meta,
    então aqui podemos tomar nosso tempo: digitar, pensar, responder.

    Se vier áudio (audio_media_id), transcreve primeiro e usa o texto resultante.
    """
    try:
        # 1) Mostra check azul + "digitando..." imediatamente
        marcar_como_lida_e_digitando(
            clinica["phone_number_id"], message_id_whatsapp
        )

        # 1.5) Se for áudio, baixa e transcreve.
        if audio_media_id:
            print(f"🎵 Transcrevendo áudio de {numero_lead}...")
            audio_bytes = baixar_audio_whatsapp(audio_media_id)
            texto_recebido = transcrever_audio(audio_bytes)

            if not texto_recebido:
                # Não conseguiu transcrever — avisa o lead e desiste dessa mensagem.
                enviar_mensagem_whatsapp(
                    clinica["phone_number_id"], numero_lead,
                    "Não consegui entender o áudio direito. "
                    "Pode tentar de novo ou me escrever?"
                )
                return

            print(f"   Transcrição: {texto_recebido}")

        # 2) Salva a mensagem do lead no banco (texto, mesmo se veio de áudio).
        salvar_mensagem(
            conversa_id, "user", texto_recebido,
            message_id_whatsapp=message_id_whatsapp
        )

        # 2.5) Se um humano assumiu a conversa, a Ana NÃO responde.
        # A mensagem fica salva pra ele ler no painel.
        if conversa_esta_pausada(conversa_id):
            print(f"⏸️  Conversa {conversa_id} pausada (humano assumiu) — Ana em silêncio.")
            return

        # 3) Carrega o histórico recente da conversa.
        historico = obter_historico_conversa(conversa_id, limite=20)
        # A última mensagem do histórico É a que acabou de chegar.
        # A função gerar_resposta_ia vai re-adicionar ela, então tiramos daqui.
        if historico and historico[-1]["role"] == "user":
            historico_base = historico[:-1]
        else:
            historico_base = historico

        # 4) Gera resposta com Claude.
        resposta_completa = gerar_resposta_ia(
            clinica["system_prompt"], historico_base, texto_recebido
        )

        if not resposta_completa:
            telefone_humano = clinica.get("telefone_humano") or ""
            fallback = "Desculpe, tive um problema técnico. Pode tentar novamente"
            fallback += f" ou ligar no {telefone_humano}?" if telefone_humano else "?"
            enviar_mensagem_whatsapp(
                clinica["phone_number_id"], numero_lead, fallback
            )
            return

        # 5) Salva a resposta da Ana inteira no banco (uma vez).
        salvar_mensagem(conversa_id, "assistant", resposta_completa)

        # 6) Quebra em partes e envia cada uma com delay natural.
        partes = quebrar_em_partes(resposta_completa)
        for parte in partes:
            time.sleep(calcular_delay(parte))
            enviar_mensagem_whatsapp(
                clinica["phone_number_id"], numero_lead, parte
            )

        print(f"💬 [{clinica['nome']}] Ana respondeu em {len(partes)} parte(s) pra {numero_lead}")

    except Exception as e:
        print(f"❌ Erro no processamento de background: {e}")


# ============================================================
# ROTAS
# ============================================================
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verificação inicial do webhook pela Meta."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verificado!")
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def receive_message():
    """
    Recebe a mensagem da Meta e responde 200 IMEDIATAMENTE.
    O processamento real (Claude + envio) roda em thread separada.
    Isso evita que a Meta reenvie webhooks por timeout.
    """
    try:
        data = request.get_json()

        if data.get("object") != "whatsapp_business_account":
            return jsonify({"status": "ignored"}), 200

        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # MULTI-TENANT: descobre qual clínica recebeu a mensagem.
                metadata = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id")
                if not phone_number_id:
                    continue

                clinica = buscar_clinica_por_phone_id(phone_number_id)
                if not clinica:
                    print(f"⚠️  Sem clínica cadastrada pra phone_id {phone_number_id}.")
                    continue

                for message in value.get("messages", []):
                    message_id = message.get("id")
                    sender = message.get("from")
                    msg_type = message.get("type")

                    # Anti-duplicata (consulta o banco).
                    if mensagem_ja_processada(message_id):
                        print(f"⏭️  Mensagem {message_id} já processada.")
                        continue

                    if msg_type == "text":
                        texto = message.get("text", {}).get("body", "")
                        print(f"\n📨 [{clinica['nome']}] {sender}: {texto}")

                        conversa_id = obter_ou_criar_conversa(
                            clinica["id"], sender
                        )

                        # Dispara processamento em segundo plano (texto).
                        threading.Thread(
                            target=processar_mensagem_em_background,
                            kwargs={
                                "clinica": dict(clinica),
                                "conversa_id": conversa_id,
                                "message_id_whatsapp": message_id,
                                "numero_lead": sender,
                                "texto_recebido": texto,
                            },
                            daemon=True
                        ).start()

                    elif msg_type == "audio":
                        audio_info = message.get("audio", {})
                        media_id = audio_info.get("id")
                        if not media_id:
                            print(f"⚠️  Áudio sem media_id, ignorando.")
                            continue

                        print(f"\n🎵 [{clinica['nome']}] áudio de {sender} (media_id={media_id})")

                        conversa_id = obter_ou_criar_conversa(
                            clinica["id"], sender
                        )

                        # Dispara processamento em segundo plano (áudio → transcrição → resposta).
                        threading.Thread(
                            target=processar_mensagem_em_background,
                            kwargs={
                                "clinica": dict(clinica),
                                "conversa_id": conversa_id,
                                "message_id_whatsapp": message_id,
                                "numero_lead": sender,
                                "audio_media_id": media_id,
                            },
                            daemon=True
                        ).start()

                    else:
                        print(f"ℹ️  Tipo '{msg_type}' não tratado.")
                        enviar_mensagem_whatsapp(
                            phone_number_id, sender,
                            "Por enquanto consigo ler apenas mensagens de texto e áudio. "
                            "Pode me escrever?"
                        )

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ Erro no webhook: {e}")
        # Mesmo em erro, responde 200 — não queremos a Meta reenviando.
        return jsonify({"status": "error"}), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "online",
        "service": "WhatsApp Ana (multi-tenant)",
        "ai": CLAUDE_MODEL,
    })


# ============================================================
# INICIALIZAÇÃO DO BANCO + REGISTRO DO PAINEL
# ============================================================
try:
    inicializar_banco()
    print("✅ Banco de dados pronto.")
except Exception as e:
    print(f"⚠️  Erro ao inicializar banco: {e}")

# Anexa todas as rotas do painel web ao app principal.
registrar_rotas_painel(app)
print("✅ Painel web registrado em /painel")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
