"""
Lead State

Representa a etapa atual do cliente.
"""

from enum import Enum


class LeadState(str, Enum):
    NEW = "new"

    INTERESTED = "interested"

    QUALIFIED = "qualified"

    BUYING = "buying"

    CUSTOMER = "customer"
