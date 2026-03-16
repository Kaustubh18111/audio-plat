import os
from PIL import Image, ImageDraw
from term_image.image import from_file

def test_ghostty_local():
    print("="*50)
    print(" 👻 GHOSTTY LOCAL GRAPHICS TEST 👻 ")
    print("="*50)
    
    temp_path = "test_local.png"
    
    print("[*] Generating a test image locally...")
    try:
        # Create a simple red square with a blue circle
        img = Image.new('RGB', (200, 200), color='red')
        d = ImageDraw.Draw(img)
        d.ellipse((50, 50, 150, 150), fill=(0, 0, 255))
        img.save(temp_path)
        print("[*] Local image generated successfully.")
        
        print("[*] Pushing image buffer to Ghostty terminal...")
        image = from_file(temp_path)
        image.draw()
        print("\n[+] SUCCESS: If you see a Red square with a Blue circle above, Ghostty graphics are working!")
        
    except Exception as e:
        print(f"\n[-] Graphics Error: {e}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

if __name__ == "__main__":
    test_ghostty_local()
    