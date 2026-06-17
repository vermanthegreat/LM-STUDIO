import os
import json
import requests
from openai import OpenAI
from pathlib import Path

# --- PODEŠAVANJA (PROMENI OVO) ---
RADNI_DIREKTORIJUM = "C:/Users/TvojKorisnickiNalog/Documents/MojiKontakti" # ⚠️ PROMENI OVO: Putanja do tvog radnog direktorijuma

# Povezivanje sa LM Studio serverom
client = OpenAI(
    base_url="http://localhost:1234/v1", # Adresa LM Studio servera
    api_key="lm-studio"                   # API ključ (nije potreban za lokalni server)
)

# Definicija alata koje će naš LLM moći da koristi
# Ovo opisujemo modelu na način koji on razume
tools = [
    {
        "type": "function",
        "function": {
            "name": "list_files_in_directory",
            "description": "Listuje sve fajlove u datom direktorijumu.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory_path": {
                        "type": "string",
                        "description": "Putanja do direktorijuma (npr. 'radni_dir/kontakti').",
                    }
                },
                "required": ["directory_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Čita sadržaj tekst fajla.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Putanja do fajla (npr. 'radni_dir/kontakti/lista.txt').",
                    }
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Upisuje sadržaj u tekst fajl.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Putanja do fajla."},
                    "content": {"type": "string", "description": "Sadržaj koji treba upisati."},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    # Ovde ćeš kasnije dodati definiciju za tvoju "sortiraj" funkciju
]


# --- Implementacija alata (stvarna Python logika) ---
def list_files_in_directory(directory_path: str) -> str:
    """Vraća listu fajlova u direktorijumu kao string."""
    full_path = Path(C:\Users\verman\Desktop\LM STUDIO\CommerceGov) / directory_path
    try:
        files = [f.name for f in full_path.iterdir() if f.is_file()]
        return json.dumps({"ok": True, "files": files})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

def read_file(file_path: str) -> str:
    """Čita sadržaj fajla i vraća ga kao string."""
    full_path = Path(RADNI_DIREKTORIJUM) / file_path
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return json.dumps({"ok": True, "content": content})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

def write_file(file_path: str, content: str) -> str:
    """Upisuje sadržaj u fajl."""
    full_path = Path(RADNI_DIREKTORIJUM) / file_path
    try:
        # Prvo kreiraj direktorijume ako ne postoje
        full_path.parent.mkdir(parents=True, exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return json.dumps({"ok": True, "message": f"Fajl uspešno upisan na {full_path}"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


# --- Glavna petlja za interakciju ---
def pokreni_asistenta():
    """Glavna funkcija koja pokreće asistenta i obrađuje zahteve."""
    print(f"Asistent je spreman! Radni direktorijum: {RADNI_DIREKTORIJUM}")
    
    # Lista poruka koja će čuvati istoriju konverzacije
    conversation_history = [
        {"role": "system", "content": f"Ti si asistent koji može da manipuliše fajlovima. Tvoj radni direktorijum je {RADNI_DIREKTORIJUM}. Kad god ti zatreba, pozovi odgovarajuću funkciju. Koristi relativne putanje u odnosu na radni direktorijum."}
    ]

    while True:
        user_input = input("\nTi: ")
        if user_input.lower() in ["izlaz", "exit", "quit"]:
            break

        conversation_history.append({"role": "user", "content": user_input})

        # Šaljemo zahtev LM Studio modelu
        response = client.chat.completions.create(
            model="lmstudio-community/Llama-3-Groq-8B-Tool-Use-GGUF", # Zameni sa tačnim imenom tvog modela
            messages=conversation_history,
            tools=tools,
            tool_choice="auto" # Model sam odlučuje da li će pozvati alat
        )

        response_message = response.choices[0].message
        conversation_history.append(response_message)

        # Proveravamo da li je model tražio da pozovemo neki alat
        if response_message.tool_calls:
            for tool_call in response_message.tool_calls:
                function_name = tool_call.function.name
                function_args = json.loads(tool_call.function.arguments)

                print(f"🧠 Asistent želi da pozove funkciju: {function_name} sa parametrima: {function_args}")

                # Pozivamo odgovarajuću Python funkciju
                if function_name == "list_files_in_directory":
                    function_response = list_files_in_directory(**function_args)
                elif function_name == "read_file":
                    function_response = read_file(**function_args)
                elif function_name == "write_file":
                    function_response = write_file(**function_args)
                else:
                    function_response = json.dumps({"ok": False, "error": f"Nepoznata funkcija: {function_name}"})

                # Rezultat funkcije prosleđujemo nazad modelu
                conversation_history.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": function_response,
                })

            # Nakon što smo izvršili sve funkcije, ponovo pitamo model za konačan odgovor
            second_response = client.chat.completions.create(
                model="lmstudio-community/Llama-3-Groq-8B-Tool-Use-GGUF",
                messages=conversation_history,
            )
            final_message = second_response.choices[0].message.content
            print(f"Asistent: {final_message}")
            conversation_history.append({"role": "assistant", "content": final_message})
        else:
            # Ako model nije tražio alat, samo ispisujemo njegov odgovor
            print(f"Asistent: {response_message.content}")

if __name__ == "__main__":
    pokreni_asistenta()