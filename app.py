from flask import Flask, request, jsonify
import os
import requests
from datetime import datetime

app = Flask(__name__)

# ============================================================
# CONFIGURAÇÕES (todas vêm das variáveis de ambiente do Render)
# ============================================================
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "meu_token_secreto_123")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

# Modelo da IA.
# claude-haiku-4-5-20251001  -> rápido e barato (recomendado p/ atendimento)
# claude-sonnet-4-6          -> mais sofisticado, troque aqui se quiser mais nuance
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# ============================================================
# ARMAZENAMENTO EM MEMÓRIA
# ATENÇÃO: zera quando o servidor dorme (free tier) ou faz redeploy.
# Migrar p/ Redis/PostgreSQL antes de produção séria.
# ============================================================
conversas = {}              # conversas[numero] = [{"role": ..., "content": ...}]
mensagens_processadas = set()  # IDs de mensagens já respondidas (anti-duplicata)

# ============================================================
# PROMPT DA ANA (vai no campo "system", não misturado no histórico)
# ============================================================
SYSTEM_PROMPT = """Você é Ana, secretária virtual da MB Odontologia Especializada em Mogi Guaçu/SP.

# SEU PERFIL
Você é uma profissional experiente que entende de psicologia do paciente odontológico. Sabe ler nas entrelinhas, captar inseguranças, identificar objeções reais vs desculpas, e conduzir conversas de forma natural e estratégica.

**Tom:** Simpática, acolhedora, objetiva. Mensagens curtas (3-4 linhas). Máximo 1 emoji por mensagem. Nunca soe como robô ou script decorado.

# INFORMAÇÕES DA CLÍNICA
**Localização:** Rua Mário Vedovello, 72 - Parque São Luiz, Mogi Guaçu/SP
**Horários:** Segunda, terça e sábado - 8h às 19h
**WhatsApp:** +55 19 97825-1938 (apenas mensagens)
**Telefone:** +55 19 99343-6676 (atendimento humano se solicitado)
**Email:** odontologiaespecializadamb@gmail.com

**Especialidades principais:**
- Lentes de Resina (Dra. Maryah - 15 anos de experiência, olhar estético diferenciado)
- Alinhadores Invisíveis Esthetic Aligner (correção discreta)
- Implantes Dentários (Dr. Matheus - +200 casos, reabilitação completa)
- Ortodontia

**Diferenciais:**
- Primeira consulta GRATUITA
- Atendimento exclusivo e personalizado
- Resultados naturais e harmoniosos
- Parcelamento: Pix, cartão, boleto
- Não aceitamos convênio (trabalho personalizado)

# ESTRATÉGIA DE ATENDIMENTO

**PRINCÍPIOS:**
1. Escuta ativa: identifique o que o lead REALMENTE quer
2. Qualificação inteligente: faça perguntas que revelem urgência, orçamento, objeções
3. Construa valor antes de investimento: mostre experiência, resultado, diferencial
4. Conduza, não empurre: agendamento é consequência natural

**LEITURA DE SINAIS:**
- Pergunta preço direto = já pesquisou, tem objeção de valor
- "Vou pensar" = insegurança ou não viu valor
- Compara com franquia = busca validação
- Pergunta sobre dor/medo = trauma anterior
- "Vale a pena?" = quer ser convencido

**QUEBRA DE OBJEÇÕES (PRINCÍPIOS):**

Objeção de preço:
- Não justifique preço, ELEVE valor percebido
- Mostre custo do "barato": refazer, resultado ruim
- Reforce: consulta gratuita = sem risco

Insegurança/medo:
- Valide o sentimento
- Mostre experiência como segurança
- Ofereça avaliação sem compromisso

Comparação com concorrentes:
- Não ataque concorrentes
- Diferencie: exclusividade, personalização, experiência
- Pergunte: "Preço ou resultado natural?"

Procrastinação:
- Descubra o motivo real (medo? dinheiro? tempo?)
- Remova fricção: gratuita, 30min, sem compromisso
- Urgência suave: "Agenda enchendo"

**FLUXO IDEAL:**
1. Saudação + identificação da necessidade
2. Perguntas estratégicas
3. Apresentação do diferencial relevante
4. Condução pro agendamento
5. Se objeção: empatia + lógica + valor

**NUNCA:**
- Dar valores (sempre "depende, a avaliação gratuita mostra")
- Mensagens longas
- Excesso de emojis
- Forçar agendamento quando o lead não está pronto
- Comparações agressivas com concorrentes

Responda sempre como Ana, em no máximo 4 linhas."""


# ============================================================
# WHATSAPP
# ============================================================
def enviar_mensagem_whatsapp(numero_destino, mensagem):
    """Envia mensagem via WhatsApp Cloud API."""
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "text",
        "text": {"body": mensagem},
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=20)
        response.raise_for_status()
        print(f"✅ Mensagem enviada para {numero_destino}")
        return True
    except Exception as e:
        print(f"❌ Erro ao enviar mensagem: {str(e)}")
        if 'response' in locals():
            print(f"   Resposta da Meta: {response.text}")
        return False


# ============================================================
# HISTÓRICO
# ============================================================
def obter_historico_conversa(numero):
    if numero not in conversas:
        conversas[numero] = []
    return conversas[numero]


def adicionar_ao_historico(numero, role, content):
    if numero not in conversas:
        conversas[numero] = []
    conversas[numero].append({"role": role, "content": content})
    # Mantém as últimas 20 mensagens (10 trocas)
    if len(conversas[numero]) > 20:
        conversas[numero] = conversas[numero][-20:]


# ============================================================
# CLAUDE (chamada direta via requests — sem SDK)
# ============================================================
def responder_com_claude(numero_usuario, mensagem_usuario):
    """Gera a resposta da Ana usando a API da Anthropic."""
    try:
        historico = obter_historico_conversa(numero_usuario)

        # Monta as mensagens no formato da API: histórico + mensagem atual.
        # O prompt da Ana vai no campo "system", separado.
        messages = list(historico)  # cópia do histórico já no formato role/content
        messages.append({"role": "user", "content": mensagem_usuario})

        headers = {
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": CLAUDE_MODEL,
            "max_tokens": 400,
            "temperature": 0.7,
            "system": SYSTEM_PROMPT,
            "messages": messages,
        }

        response = requests.post(
            ANTHROPIC_API_URL, headers=headers, json=payload, timeout=30
        )

        # Se der erro, mostra o corpo completo no log — diagnóstico fica óbvio.
        if response.status_code != 200:
            print(f"❌ API Claude retornou {response.status_code}: {response.text}")
            return ("Desculpe, tive um problema técnico. "
                    "Pode tentar novamente ou ligar no (19) 99343-6676?")

        data = response.json()
        resposta = data["content"][0]["text"].strip()

        # Atualiza histórico só depois de sucesso
        adicionar_ao_historico(numero_usuario, "user", mensagem_usuario)
        adicionar_ao_historico(numero_usuario, "assistant", resposta)

        print(f"💬 Ana respondeu: {resposta}")
        return resposta

    except Exception as e:
        print(f"❌ Erro ao gerar resposta: {str(e)}")
        return ("Desculpe, tive um problema técnico. "
                "Pode tentar novamente ou ligar no (19) 99343-6676?")


# ============================================================
# ROTAS
# ============================================================
@app.route('/webhook', methods=['GET'])
def verify_webhook():
    """Verificação do webhook pela Meta."""
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')

    if mode == 'subscribe' and token == VERIFY_TOKEN:
        print("✅ Webhook verificado!")
        return challenge, 200
    print("❌ Falha na verificação")
    return 'Forbidden', 403


@app.route('/webhook', methods=['POST'])
def receive_message():
    """Recebe mensagens e responde com a Ana."""
    try:
        data = request.get_json()

        if data.get("object") == "whatsapp_business_account":
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})

                    if "messages" in value:
                        for message in value["messages"]:
                            message_id = message.get("id")

                            # Anti-duplicata: a Meta reenvia webhooks.
                            # Se já processamos este ID, ignora.
                            if message_id in mensagens_processadas:
                                print(f"⏭️  Mensagem {message_id} já processada, ignorando.")
                                continue
                            mensagens_processadas.add(message_id)

                            sender = message.get("from")
                            msg_type = message.get("type")

                            if msg_type == "text":
                                texto = message.get("text", {}).get("body", "")
                                print(f"\n📨 Nova mensagem de {sender}: {texto}")
                                resposta = responder_com_claude(sender, texto)
                                enviar_mensagem_whatsapp(sender, resposta)
                            else:
                                # Tipo não suportado (áudio, imagem, etc.)
                                print(f"ℹ️  Mensagem tipo '{msg_type}' não tratada.")
                                enviar_mensagem_whatsapp(
                                    sender,
                                    "Por enquanto consigo ler apenas mensagens de texto. "
                                    "Pode me escrever? 😊"
                                )

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ Erro: {str(e)}")
        return jsonify({"status": "error"}), 500


@app.route('/', methods=['GET'])
def home():
    """Status do servidor."""
    return jsonify({
        "status": "online",
        "service": "WhatsApp Ana - MB Odontologia",
        "ai": CLAUDE_MODEL,
        "conversas_ativas": len(conversas),
    })


if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
