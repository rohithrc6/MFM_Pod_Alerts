"""One-time helper: upload email_assets/image.png to Cloudinary.

Run manually once (not part of the recurring schedule):
    python3 tools/upload_header_image.py

--- EXECUTION STRATEGY ---
Authenticates to Cloudinary using the three credentials from .env
(CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET),
uploads email_assets/image.png with a fixed public_id of "mfm_email_header"
so re-running this script always replaces the same asset at the same URL
rather than creating a new one. Prints the resulting secure HTTPS URL, which
must be manually copied into .env as EMAIL_HEADER_IMAGE_URL. build_email.py
reads that variable on every recurring run; this script only needs to be
re-run if you swap out the header image.
"""
import os
import sys

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE_PATH = os.path.join(ROOT_DIR, "email_assets", "image.png")


def main():
    """Authenticate to Cloudinary and upload the header image.

    Input:  none -- reads CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY,
            CLOUDINARY_API_SECRET from .env, and the image from
            email_assets/image.png on disk.
    Output: none -- prints the uploaded image's public secure_url to stdout
            so it can be copied into .env as EMAIL_HEADER_IMAGE_URL.
    Exits:  1 if any required credential is missing or the image file is
            not found at the expected path.
    """
    load_dotenv(os.path.join(ROOT_DIR, ".env"))

    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME")
    api_key = os.environ.get("CLOUDINARY_API_KEY")
    api_secret = os.environ.get("CLOUDINARY_API_SECRET")
    if not all([cloud_name, api_key, api_secret]):
        print(
            "CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET "
            "must all be set in .env. Add them and retry.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not os.path.exists(IMAGE_PATH):
        print(f"Image not found at {IMAGE_PATH}", file=sys.stderr)
        sys.exit(1)

    cloudinary.config(cloud_name=cloud_name, api_key=api_key, api_secret=api_secret)

    result = cloudinary.uploader.upload(
        IMAGE_PATH,
        public_id="mfm_email_header",
        overwrite=True,
        resource_type="image",
    )

    url = result["secure_url"]
    print("Upload successful.")
    print(f"Add this to .env:\nEMAIL_HEADER_IMAGE_URL={url}")


if __name__ == "__main__":
    main()
