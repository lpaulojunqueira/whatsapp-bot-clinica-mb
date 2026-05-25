from flask import Flask, request, jsonify
import os
import hmac
import hashlib

app = Flask(__name__)

# Configurações
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "meu_token_secreto_123")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

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
    """Recebe mensagens do WhatsApp"""
    try:
        data = request.get_json()
        print(f"Mensagem recebida: {data}")
        
        # Extrai informações da mensagem
        if data.get("object") == "whatsapp_business_account":
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    
                    # Verifica se tem mensagem
                    if "messages" in value:
                        for message in value["messages"]:
                            sender = message.get("from")
                            text = message.get("text", {}).get("body", "")
                            
                            print(f"De: {sender}")
                            print(f"Mensagem: {text}")
                            
                            # Aqui você vai integrar com Claude depois
                            # Por enquanto, só loga a mensagem
        
        return jsonify({"status": "success"}), 200
    
    except Exception as e:
        print(f"Erro: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    """Rota principal"""
    return jsonify({
        "status": "online",
        "service": "WhatsApp Webhook - Clínica MB"
    })

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=True)
