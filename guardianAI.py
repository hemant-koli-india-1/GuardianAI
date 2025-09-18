import os
import time
import json
import shutil
import subprocess
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import httpx
from langchain_groq import ChatGroq
from langchain_core.prompts import PromptTemplate
from dotenv import load_dotenv

import win32security

load_dotenv()

def get_owner(path):
    try:
        sd = win32security.GetFileSecurity(path, win32security.OWNER_SECURITY_INFORMATION)
        owner_sid = sd.GetSecurityDescriptorOwner()
        name, domain, _ = win32security.LookupAccountSid(None, owner_sid)
        return f"{domain}\\{name}"
    except Exception as e:
        print(f"[WARNING] Could not get owner for {path}: {e}")
        return "Unknown"

class Chain:
    def __init__(self):
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("Missing GROQ_API_KEY environment variable")

        custom_http_client = httpx.Client(verify=False, timeout=15.0)

        self.llm = ChatGroq(
            temperature=0,
            groq_api_key=api_key,
            model_name="llama-3.3-70b-versatile",
            http_client=custom_http_client
        )

    def check_names(self, name):
        prompt = '''
        You are an Indian assistant to check if there is any personal name in the file or folder name "{file_name}".  
        Only answer in the exact JSON format: 
        {{ "name_found": true or false }}
        '''
        prompt_template = PromptTemplate(
            input_variables=["file_name"], 
            template=prompt
        )
        llm_chain = prompt_template | self.llm

        try:
            result = llm_chain.invoke({"file_name": name})
            raw_response = result.content if hasattr(result, "content") else result
            return raw_response
        except Exception as e:
            print(f"[ERROR] LLM call failed for '{name}': {e}")
            return '{"name_found": false}'

# This is your user-to-machine mapping based on the details you provided
user_to_machine = {
    "APAC\\HEKOLLI": "C185LX082414174",
    "APAC\\KHAIRES": "C185LX083361454",
    "APAC\\SURESAD": "C185LX091093328",
    "APAC\\SONIARN": "C185LX091074664"
}

def send_msg_to_user(client_machine, username, message):
    try:
        cmd = ['msg', f'/server:{client_machine}', username, message]
        subprocess.run(cmd, check=True)
        print(f"[INFO] Sent message to {username}@{client_machine}")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Failed to send msg: {e}")

class WatcherHandler(FileSystemEventHandler):
    def __init__(self, chain, quarantine_dir=None):
        super().__init__()
        self.chain = chain
        self.quarantine_dir = quarantine_dir
    
    def on_rm_error(self, func, path, exc_info):
        """Error handler for shutil.rmtree."""
        import stat
        # Change file attributes and try again
        try:
            os.chmod(path, stat.S_IWRITE)
            if os.path.isdir(path):
                os.rmdir(path)
            else:
                os.unlink(path)
            print(f"[ACTION] Fixed permissions and removed: {path}")
        except Exception as e:
            print(f"[ERROR] Could not remove {path}: {e}")

    def delete_path(self, path, is_folder, max_retries=3, retry_delay=2):
        """
        Attempt to delete a file or folder with retries.
        If deletion fails after retries and quarantine_dir is set, move it there.
        """
        if not os.path.exists(path):
            print(f"[INFO] Path does not exist: {path}")
            return False

        for attempt in range(1, max_retries + 1):
            try:
                # Try to fix permissions first (like you already do)
                try:
                    if is_folder:
                        for root, dirs, files in os.walk(path):
                            for d in dirs:
                                os.chmod(os.path.join(root, d), 0o777)
                            for f in files:
                                os.chmod(os.path.join(root, f), 0o777)
                        os.chmod(path, 0o777)
                    else:
                        os.chmod(path, 0o777)
                except Exception as e:
                    print(f"[WARNING] Could not modify permissions: {e}")

                # Try deleting
                if is_folder:
                    shutil.rmtree(path, onerror=self.on_rm_error)
                else:
                    os.remove(path)

                print(f"[ACTION] Successfully deleted {'folder' if is_folder else 'file'}: {path}")
                return True

            except Exception as e:
                print(f"[WARNING] Attempt {attempt} failed to delete {path}: {e}")

                if attempt < max_retries:
                    time.sleep(retry_delay)
                else:
                    # After max retries fail:
                    if self.quarantine_dir:
                        try:
                            os.makedirs(self.quarantine_dir, exist_ok=True)
                            basename = os.path.basename(path)
                            quarantine_path = os.path.join(self.quarantine_dir, basename)
                            shutil.move(path, quarantine_path)
                            print(f"[ACTION] Moved to quarantine folder: {quarantine_path}")
                            return True
                        except Exception as move_err:
                            print(f"[ERROR] Failed to move to quarantine: {move_err}")
                    print(f"[ERROR] Failed to delete {path} after {max_retries} attempts.")
                    return False

    def process_name(self, path, is_folder=False):
        name = os.path.basename(path)
        typ = "Folder" if is_folder else "File"
        owner = get_owner(path)
        print(f"{typ} detected: {name} (Owner: {owner})")

        raw_response = self.chain.check_names(name)
        try:
            response_json = json.loads(raw_response)
            name_found = response_json.get("name_found", False)
        except json.JSONDecodeError:
            print(f"[WARNING] Invalid JSON from LLM for '{name}': {raw_response}")
            name_found = False

        if name_found:
            print(f"[ALERT] Personal name found in {typ.lower()} name: '{name}' (Owner: {owner})")

            # Delete the file/folder (no quarantine_dir param here)
            self.delete_path(path, is_folder)

            # Send popup message to user if mapping exists
            client_machine = user_to_machine.get(owner)
            if client_machine:
                username_only = owner.split("\\")[-1]
                message = (f"The {typ.lower()} name '{name}' contains a personal name and violates naming guidelines.\n"
                           "It has been removed. Please rename it according to the policy.")
                send_msg_to_user(client_machine, username_only, message)
            else:
                print(f"[WARNING] No client machine mapping found for user {owner}, cannot send notification.")
        else:
            print(f"[INFO] No personal name detected in {typ.lower()} name: '{name}'")

    def on_created(self, event):
        self.process_name(event.src_path, is_folder=event.is_directory)

    def on_moved(self, event):
        self.process_name(event.dest_path, is_folder=event.is_directory)

def main():
    folder_to_watch = r"\\s185f0024\tids\A185_MO_India\01_Team_Specific_Doc\02_Supply Chain\01_PPC"
    quarantine_folder = r"\\s185f0024\tids\Quarantine_Folder"

    chain = Chain()
    event_handler = WatcherHandler(chain, quarantine_dir=quarantine_folder)
    observer = Observer()
    observer.schedule(event_handler, folder_to_watch, recursive=True)
    observer.start()
    print(f"Monitoring started on {folder_to_watch}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping monitoring...")
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()
