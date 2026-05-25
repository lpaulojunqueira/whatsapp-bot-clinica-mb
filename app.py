from flask import Flask, request, jsonify
import os
import requests
from anthropic import Anthropic
from datetime import datetime

app = Flask(__name__)

# Configurações
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "meu_token_secreto_123")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

# Cliente Anthropic
anthropic_client = Anthropic(api_key=CLAUDE_API_KEY)

# Armazenamento simples de conversas (em memória)
conversas = {}

def enviar_mensagem_whatsapp(numero_destino, mensagem):
    """Envia mensagem via WhatsApp API"""
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    
    data = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "text",
        "text": {"body": mensagem}
    }
    
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        print(f"✅ Mensagem enviada para {numero_destino}")
        return True
    except Exception as e:
        print(f"❌ Erro ao enviar mensagem: {str(e)}")
        return False

def obter_historico_conversa(numero):
    """Obtém histórico da conversa com um número"""
    if numero not in conversas:
        conversas[numero] = []
    return conversas[numero]

def adicionar_ao_historico(numero, role, content):
    """Adiciona mensagem ao histórico"""
    if numero not in conversas:
        conversas[numero] = []
    conversas[numero].append({"role": role, "content": content})
    
    # Limita histórico a últimas 20 mensagens (10 trocas)
    if len(conversas[numero]) > 20:
        conversas[numero] = conversas[numero][-20:]

def formatar_historico_para_prompt(historico):
    """Formata histórico para o prompt"""
    if not historico:
        return "Primeira mensagem do paciente."
    
    texto = ""
    for msg in historico[-10:]:  # Últimas 5 trocas
        role_nome = "Paciente" if msg["role"] == "user" else "Ana"
        texto += f"{role_nome}: {msg['content']}\n"
    return texto.strip()

def responder_com_claude(numero_usuario, mensagem_usuario):
    """Gera resposta usando Claude com contexto"""
    try:
        # Obtém histórico
        historico = obter_historico_conversa(numero_usuario)
        historico_formatado = formatar_historico_para_prompt(historico)
        
        # Prompt completo
        prompt = f"""Você é Ana, secretária virtual da MB Odontologia Especializada em Mogi Guaçu/SP.

# SEU PERFIL
Você é uma profissional experiente que entende de psicologia do paciente odontológico. Sabe ler nas entrelinhas, captar inseguranças, identificar objeções reais vs desculpas, e conduzir conversas de forma natural e estratégica.

**Tom:** Simpática, acolhedora, objetiva. Mensagens curtas (3-4 linhas). Máximo 1 emoji por mensagem.

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
1. **Escuta ativa:** Identifique o que o lead REALMENTE quer
2. **Qualificação inteligente:** Faça perguntas que revelem urgência, orçamento, objeções
3. **Construa valor antes de investimento:** Mostre experiência, resultado, diferencial
4. **Conduza, não empurre:** Agendamento é consequência natural

**LEITURA DE SINAIS:**
- Pergunta preço direto = já pesquisou, tem objeção de valor
- "Vou pensar" = insegurança ou não viu valor
- Compara com franquia = busca validação
- Pergunta sobre dor/medo = trauma anterior
- "Vale a pena?" = quer ser convencido

**QUEBRA DE OBJEÇÕES (PRINCÍPIOS):**

**Objeção de preço:**
- Não justifique preço, ELEVE valor percebido
- Mostre custo do "barato": refazer, resultado ruim
- Reforce: consulta gratuita = sem risco

**Insegurança/medo:**
- Valide sentimento
- Mostre experiência como segurança
- Ofereça avaliação sem compromisso

**Comparação com concorrentes:**
- Não ataque concorrentes
- Diferencie: exclusividade, personalização, experiência
- Pergunte: "Preço ou resultado natural?"

**Procrastinação:**
- Descubra motivo real (medo? dinheiro? tempo?)
- Remova fricção: gratuita, 30min, sem compromisso
- Urgência suave: "Agenda enchendo"

**FLUXO IDEAL:**
1. Saudação + Identificação da necessidade
2. Perguntas estratégicas
3. Apresentação do diferencial relevante
4. Condução pro agendamento
5. Se objeção: empatia + lógica + valor

**NUNCA:**
- Dar valores (sempre "depende, avaliação gratuita")
- Mensagens longas
- Excesso de emojis
- Forçar agendamento
- Comparações agressivas

---

Histórico da conversa:
{historico_formatado}

Mensagem atual do paciente: {mensagem_usuario}

Responda como Ana (máximo 4 linhas):"""

        # Chamada ao Claude
        message = anthropic_client.messages.create(
            model="claude-3-5-sonnet-latest",
            max_tokens=400,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}]
        )
        
        resposta = message.content[0].text.strip()
        
        # Adiciona ao histórico
        adicionar_ao_historico(numero_usuario, "user", mensagem_usuario)
        adicionar_ao_historico(numero_usuario, "assistant", resposta)
        
        print(f"💬 Ana respondeu: {resposta}")
        return resposta
        
    except Exception as e:
        print(f"❌ Erro ao gerar resposta: {str(e)}")
        return "Desculpe, tive um problema técnico. Pode tentar novamente ou ligar no (19) 99343-6676?"

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    """Verificação do webhook pela Meta"""
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        print("✅ Webhook verificado!")
        return challenge, 200
    else:
        print("❌ Falha na verificação")
        return 'Forbidden', 403

@app.route('/webhook', methods=['POST'])
def receive_message():
    """Recebe mensagens e responde com Ana"""
    try:
        data = request.get_json()
        
        if data.get("object") == "whatsapp_business_account":
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    
                    if "messages" in value:
                        for message in value["messages"]:
                            sender = message.get("from")
                            msg_type = message.get("type")
                            
                            if msg_type == "text":
                                texto = message.get("text", {}).get("body", "")
                                
                                print(f"\n📨 Nova mensagem de {sender}: {texto}")
                                
                                # Gera resposta com Claude
                                resposta = responder_com_claude(sender, texto)
                                
                                # Envia resposta
                                enviar_mensagem_whatsapp(sender, resposta)
        
        return jsonify({"status": "success"}), 200
    
    except Exception as e:
        print(f"❌ Erro: {str(e)}")
        return jsonify({"status": "error"}), 500

@app.route('/', methods=['GET'])
def home():
    """Status do servidor"""
    return jsonify({
        "status": "online",
        "service": "WhatsApp Ana - MB Odontologia",
        "ai": "Claude Sonnet 4",
        "conversas_ativas": len(conversas)
    })

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=True)
