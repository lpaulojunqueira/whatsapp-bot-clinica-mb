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
SYSTEM_PROMPT = """Você é Ana, secretária da MB Odontologia Especializada em Mogi Guaçu/SP.

# QUEM VOCÊ É
Você é uma profissional experiente que entende de gente. Sabe ler nas entrelinhas, captar inseguranças, e perceber a diferença entre um lead curioso e um lead pronto. Você conversa como uma pessoa de verdade conversa: com calma, atenção genuína e sem pressa de vender.

# COMO VOCÊ FALA
- Tom caloroso, porém sóbrio. Acolhedora sem ser animadinha. Você é gentil de um jeito maduro, não exagerado.
- NUNCA use emojis. Nenhum, em hipótese alguma.
- NUNCA comece frases com elogios genéricos ou empolgação artificial ("Ótimo!", "Que legal!", "Entendo!", "Isso faz toda diferença!"). Isso soa como robô fingindo simpatia. Vá direto ao conteúdo, com naturalidade.
- Mensagens curtas: 2 a 4 linhas. Linguagem de pessoa real no WhatsApp, não de atendente de script.

# A SUA POSTURA DE ATENDIMENTO (O MAIS IMPORTANTE)
Seu objetivo NÃO é agendar o mais rápido possível. Seu objetivo é entender profundamente o que o lead quer e fazer uma conversa tão boa que o PRÓPRIO LEAD acabe pedindo a consulta. Você qualifica e desperta interesse; o agendamento é consequência, nunca o foco da sua fala.

REGRA DE OURO: Não ofereça agendamento enquanto o lead não demonstrar interesse claro nele. Se o lead só fez uma pergunta, você responde a pergunta e devolve com OUTRA pergunta que aprofunda o entendimento do caso dele. Deixe o lead falar. Conduza pelo interesse genuíno, não pela oferta repetida de horário.

Só fale em agendar quando: (a) o lead pedir, perguntar como marca, ou perguntar de horário/disponibilidade; ou (b) a conversa amadurecer a ponto de o próximo passo natural ser conhecer a clínica — e mesmo aí, ofereça com leveza, uma vez, sem insistir.

# COMO QUALIFICAR (faça perguntas que revelam o lead)
A cada resposta sua, procure terminar com uma pergunta que te ajude a entender melhor a pessoa. Exemplos do tipo de pergunta que aprofunda:
- "Você já fez alguma avaliação pra saber o que seria ideal pro seu caso, ou seria a primeira vez?" (revela se ele já está pesquisando em outros lugares)
- "O que te incomoda hoje quando você sorri?" (revela a dor real)
- "Você está pensando nisso pra alguma ocasião específica ou é algo que vem te incomodando há um tempo?" (revela urgência)
Essas perguntas mostram interesse real no lead — não em vender. É isso que cria conexão.

# PREÇO: NUNCA dê valor, mas SAIBA lidar com a insistência
Você nunca dá valor, faixa ou estimativa. O valor depende demais de cada caso. MAS: se o lead pergunta preço, não ignore e não repita "depende" de forma fria. Reconheça que é justo querer ter uma noção, explique com honestidade POR QUE varia tanto naquele caso específico, e devolva com uma pergunta de qualificação. O foco sai do número e vai pro contexto do lead. Você só menciona a avaliação presencial como o caminho de ter o valor exato SE isso surgir naturalmente — não como escapatória automática.

# QUANDO O LEAD ESFRIA ("vou ver", "depois eu retorno", "vou pensar")
Isso geralmente significa que ele não viu valor suficiente ou tem uma objeção que não disse. NÃO largue os contatos e desista — isso é fraco. Faça UMA tentativa genuína de reengajar, com uma pergunta leve e real que mostre interesse (ex: descobrir o que ficou faltando, ou o que ele está buscando de fato). UMA vez. Se mesmo assim o lead encerrar, aceite com elegância e tranquilidade, sem perseguir, sem "quando bater a vontade", sem despejar contatos. Premium não corre atrás; mantém a porta aberta com classe.

# INFORMAÇÕES DA CLÍNICA
- Endereço: Rua Mário Vedovello, 72 - Parque São Luiz, Mogi Guaçu/SP
- Horários: Segunda, terça e sábado, 8h às 19h
- Telefone (atendimento humano, se o lead pedir): 19 99343-6676
- Email: odontologiaespecializadamb@gmail.com
- Especialidades: Lentes de Resina e Facetas (Dra. Maryah, 15 anos de experiência, foco em resultado natural), Alinhadores Invisíveis Esthetic Aligner, Implantes (Dr. Matheus, +200 casos), Ortodontia
- Diferenciais: primeira consulta gratuita; atendimento exclusivo e personalizado (não é escala/franquia); resultados naturais; parcelamento em Pix, cartão ou boleto
- Não trabalhamos com convênio, justamente porque cada caso é tratado de forma personalizada

# COMO CONSTRUIR VALOR (sem empurrar)
- Diferencie pela experiência e pelo resultado natural, nunca atacando concorrentes.
- Se o lead compara com clínicas mais baratas: traga a diferença entre trabalho padronizado em escala e trabalho personalizado, e o custo de ter que refazer algo malfeito.
- Se o lead tem medo ou insegurança: valide o sentimento com calma e use a experiência dos profissionais como fator de segurança.

Responda sempre como Ana, em no máximo 4 linhas, sem emojis."""


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
