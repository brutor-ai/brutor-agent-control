#!/bin/bash
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# Test .env file loading
# Usage: ./test_env.sh

echo "======================================================================"
echo "Testing .env File Loading"
echo "======================================================================"
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "❌ .env file not found!"
    exit 1
fi

echo "✓ .env file found"
echo ""

# Show .env file contents (masked sensitive values)
echo "Contents of .env (sensitive values masked):"
echo "----------------------------------------------------------------------"
while IFS='=' read -r key value || [ -n "$key" ]; do
    # Skip comments and empty lines
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$key" ]] && continue
    [[ "$key" =~ ^[[:space:]]*$ ]] && continue

    # Remove inline comments and trim
    value=$(echo "$value" | sed 's/[[:space:]]*#.*$//' | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')
    key=$(echo "$key" | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')

    # Skip if key is empty after trimming
    [[ -z "$key" ]] && continue

    # Mask sensitive values
    if [[ "$key" =~ (SECRET|PASSWORD|KEY|TOKEN) ]]; then
        echo "$key=***"
    else
        echo "$key=$value"
    fi
done < .env
echo "----------------------------------------------------------------------"
echo ""

# Now load and export variables
echo "Loading variables into environment..."
loaded_count=0

while IFS='=' read -r key value || [ -n "$key" ]; do
    # Skip comments and empty lines
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$key" ]] && continue
    [[ "$key" =~ ^[[:space:]]*$ ]] && continue

    # Remove inline comments and trim whitespace
    value=$(echo "$value" | sed 's/[[:space:]]*#.*$//' | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')
    key=$(echo "$key" | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')

    # Skip if key is empty after trimming
    [[ -z "$key" ]] && continue

    # Export the variable
    export "$key=$value"
    loaded_count=$((loaded_count + 1))
done < .env

echo "✓ Loaded $loaded_count variables"
echo ""

# Check critical variables
echo "Checking critical variables:"
echo "----------------------------------------------------------------------"

check_var() {
    var_name=$1
    if [ -n "${!var_name}" ]; then
        if [[ "$var_name" =~ (SECRET|PASSWORD|KEY|TOKEN) ]]; then
            echo "✓ $var_name is set (value: ***)"
        else
            echo "✓ $var_name is set (value: ${!var_name})"
        fi
    else
        echo "✗ $var_name is NOT set"
    fi
}

check_var "JWT_SECRET"
check_var "DATABASE_URL"
check_var "GUNICORN_WORKERS"
check_var "BRUTOR_CONTROL_PLANE_DB_POOL_SIZE"
check_var "BRUTOR_CONTROL_PLANE_LISTEN_ADDR"

echo "----------------------------------------------------------------------"
echo ""

# Summary
if [ -n "$JWT_SECRET" ] && [ -n "$DATABASE_URL" ]; then
    echo "✓ All critical variables are set!"
    echo ""
    echo "You can now run:"
    echo "  ./start_production.sh"
else
    echo "✗ Some critical variables are missing!"
    echo ""
    echo "Please ensure your .env file contains:"
    echo "  - JWT_SECRET=your-secret-here"
    echo "  - DATABASE_URL=postgresql://..."
fi

echo ""
echo "======================================================================"