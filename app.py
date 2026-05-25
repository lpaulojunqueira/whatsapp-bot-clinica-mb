from flask import Flask, request, jsonify
import os
import requests
from anthropic import Anthropic

app = Flask(__name__)

# Configurações
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "meu_token_secreto_123")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

# Cliente Anthropic
anthropic_client = Anthropic(api_key=CLAUDE_API_KEY)

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
        print(f"Mensagem enviada com sucesso para {numero_destino}")
        return True
    except Exception as e:
        print(f"Erro ao enviar mensagem: {str(e)}")
        return False

def responder_com_claude(mensagem_usuario):
    """Gera resposta usando Claude"""
    try:
        prompt = f"""Você é um assistente virtual da Clínica MB Odontologia Especializada.

Seu papel:
- Responder dúvidas sobre tratamentos odontológicos (implantes, ortodontia, clareamento, harmonização facial, etc)
- Agendar consultas
- Ser cordial, profissional e empático
- Usar linguagem clara e acessível

Informações da clínica:
- Horário: Segunda a sexta, 8h às 18h
- WhatsApp: +55 19 97825-1938
- Tratamentos: Implantes, Ortodontia, Clareamento, Harmonização Facial, Odontologia Geral

Mensagem do paciente: {mensagem_usuario}

Responda de forma objetiva e útil:"""

        message = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        
        resposta = message.content[0].text
        print(f"Claude respondeu: {resposta}")
        return resposta
        
    except Exception as e:
        print(f"Erro ao gerar resposta com Claude: {str(e)}")
        return "Desculpe, estou com dificuldades no momento. Entre em contato pelo telefone +55 19 97825-1938."

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    """Verificação do webhook pela Meta"""
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        print("Webhook verificado com sucesso!")
        return challenge, 200
    else:
        print("Falha na verificação do webhook")
        return 'Forbidden', 403

@app.route('/webhook', methods=['POST'])
def receive_message():
    """Recebe mensagens do WhatsApp e responde com Claude"""
    try:
        data = request.get_json()
        print(f"Mensagem recebida: {data}")
        
        if data.get("object") == "whatsapp_business_account":
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    
                    if "messages" in value:
                        for message in value["messages"]:
                            sender = message.get("from")
                            msg_type = message.get("type")
                            
                            # Só processa mensagens de texto
                            if msg_type == "text":
                                texto = message.get("text", {}).get("body", "")
                                
                                print(f"De: {sender}")
                                print(f"Mensagem: {texto}")
                                
                                # Gera resposta com Claude
                                resposta = responder_com_claude(texto)
                                
                                # Envia resposta
                                enviar_mensagem_whatsapp(sender, resposta)
        
        return jsonify({"status": "success"}), 200
    
    except Exception as e:
        print(f"Erro: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    """Rota principal"""
    return jsonify({
        "status": "online",
        "service": "WhatsApp Webhook - Clínica MB",
        "ai": "Claude Sonnet 4"
    })

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=True)
