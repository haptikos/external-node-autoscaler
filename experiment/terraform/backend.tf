terraform {
  backend "s3" {
    # bucket = "byon-ff-{YOUr_ACCOUNT_ID}"
    key    = "byon-ff/terraform.tfstate"
    region = "eu-central-1"
    use_lockfile = true
    encrypt      = true
  }
}
